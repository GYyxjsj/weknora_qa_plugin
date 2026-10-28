"""WeKnora 知识库检索插件。

不让 WeKnora 生成答案：插件只调 ``POST /api/v1/knowledge-search``，用知识库建库时
绑定的向量模型（与 MaiBot ``embedding`` 任务配的是同一个）把原文片段拉回来，交给
麦麦自己的规划器思考作答。对外只有两个入口——规划器可调用的 ``weknora_search``
工具，和用户手敲的 ``/提问``；``/提问`` 也是先检索片段，再让 MaiBot 的 planner
模型根据片段组织答案。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

import asyncio
import uuid

from maibot_sdk import Command, Field, HookHandler, MaiBotPlugin, PluginConfigBase, Tool
from maibot_sdk.types import HookMode, ToolParameterInfo, ToolParamType

from .embedding_model import EmbeddingModel, EmbeddingModelError, resolve_embedding_model
from .weknora_client import KnowledgeChunk, SearchOptions, WeKnoraClient, WeKnoraError

_TOOL_NAME = "weknora_search"


class PluginSectionConfig(PluginConfigBase):
    """插件基础配置。"""

    __ui_label__ = "插件"
    __ui_icon__ = "cog"
    __ui_order__ = 0

    enabled: bool = Field(default=False, description="是否启用插件", json_schema_extra={"label": "启用"})
    config_version: str = Field(default="1.1.0", description="配置版本", json_schema_extra={"label": "配置版本"})


class ServiceConfig(PluginConfigBase):
    """WeKnora 服务连接配置。"""

    __ui_label__ = "服务"
    __ui_icon__ = "database"
    __ui_order__ = 1

    url: str = Field(
        default="http://127.0.0.1:8080",
        description="WeKnora 服务地址",
        json_schema_extra={"label": "服务地址"},
    )
    api_key: str = Field(
        default="",
        description="WeKnora API Key（请求头 X-API-Key），检索需要能访问目标知识库的密钥",
        json_schema_extra={"label": "API Key"},
    )
    timeout_seconds: int = Field(
        default=60,
        ge=5,
        le=600,
        description="单次检索超时（秒）",
        json_schema_extra={"label": "超时（秒）"},
    )


class RetrievalConfig(PluginConfigBase):
    """检索参数配置。"""

    __ui_label__ = "检索"
    __ui_icon__ = "search"
    __ui_order__ = 2

    knowledge_base: str = Field(
        default="",
        description="知识库名称或 ID；留空则检索租户下的全部知识库",
        json_schema_extra={"label": "知识库"},
    )
    match_count: int = Field(
        default=3,
        ge=1,
        le=200,
        description="取回的片段数量，越多上下文越长",
        json_schema_extra={"label": "片段数量"},
    )
    max_chars_per_chunk: int = Field(
        default=2000,
        ge=0,
        description="单条片段最多注入多少字符，0 表示不截断",
        json_schema_extra={"label": "片段截断长度"},
    )


class PromptConfig(PluginConfigBase):
    """提示词配置。"""

    __ui_label__ = "提示词"
    __ui_icon__ = "message-circle"
    __ui_order__ = 3

    bot_prompt: str = Field(
        default="",
        description="注入麦麦规划器上下文的提示词，说明什么时候该检索知识库；留空则不注入",
        json_schema_extra={"label": "Bot 提示词"},
    )


class WeKnoraQaConfig(PluginConfigBase):
    """WeKnora 知识库检索插件配置。"""

    plugin: PluginSectionConfig = Field(default_factory=PluginSectionConfig)
    service: ServiceConfig = Field(default_factory=ServiceConfig)
    retrieval: RetrievalConfig = Field(default_factory=RetrievalConfig)
    prompt: PromptConfig = Field(default_factory=PromptConfig)


class WeKnoraQaPlugin(MaiBotPlugin):
    """WeKnora 知识库检索插件。"""

    config_model = WeKnoraQaConfig

    def __init__(self) -> None:
        super().__init__()
        self._client: WeKnoraClient | None = None
        # 向量模型核对只做一次：核对结果只取决于两边的模型配置，与单次检索无关。
        self._embedding_checked = False

    # ---------------------------------------------------------------- #
    # 生命周期
    # ---------------------------------------------------------------- #

    async def on_load(self) -> None:
        self._client = self._build_client()
        retrieval = self.config.retrieval
        self.ctx.logger.info(
            "WeKnora 检索插件已加载：url=%s，知识库=%s，片段数=%d",
            self.config.service.url,
            retrieval.knowledge_base or "全部",
            retrieval.match_count,
        )

    async def on_unload(self) -> None:
        self._client = None
        self.ctx.logger.info("WeKnora 检索插件已卸载")

    async def on_config_update(self, scope: str, config_data: dict[str, Any], version: str) -> None:
        """配置热更新：重建客户端，让新参数立即生效。"""

        del config_data
        if scope != "self":
            self.ctx.logger.debug("忽略非插件自身配置更新：scope=%s version=%s", scope, version)
            return
        self._client = self._build_client()
        self._embedding_checked = False
        self.ctx.logger.info("配置已热更新（版本 %s），已重建客户端", version)

    # ---------------------------------------------------------------- #
    # 组件声明
    # ---------------------------------------------------------------- #

    def get_components(self) -> list[dict[str, Any]]:
        """给检索工具放宽 RPC 超时：向量召回加 rerank 可能超过默认的 60 秒。"""

        components = super().get_components()
        try:
            timeout_ms = (self.config.service.timeout_seconds + 30) * 1000
        except RuntimeError:
            return components
        for component in components:
            metadata = component.get("metadata")
            if component.get("name") == _TOOL_NAME and isinstance(metadata, dict):
                metadata["timeout_ms"] = timeout_ms
        return components

    @Tool(
        _TOOL_NAME,
        description=(
            "从 WeKnora 知识库检索原文片段，返回若干条最相关的段落，不做任何总结。"
            "需要查证游戏设定、角色、符卡、帧数、机制、术语等事实性问题时使用；"
            "拿到片段后由你自己组织答案，片段里没有的内容就如实说明。"
        ),
        parameters=[
            ToolParameterInfo(
                name="query",
                param_type=ToolParamType.STRING,
                description="检索用的查询语句，写清关键实体与术语",
                required=True,
            ),
        ],
    )
    async def handle_weknora_search(self, query: str = "", **kwargs: Any) -> dict[str, str]:
        """把知识库原文片段交回规划器，由规划器自己思考作答。"""

        del kwargs
        question = (query or "").strip()
        if not question:
            return {"name": _TOOL_NAME, "content": "查询为空，无法检索知识库。"}
        try:
            chunks = await self._retrieve(question)
        except WeKnoraError as exc:
            self.ctx.logger.error("知识库检索失败：%s", exc)
            return {"name": _TOOL_NAME, "content": f"知识库检索失败：{exc}"}
        return {"name": _TOOL_NAME, "content": self._format_chunks(question, chunks)}

    @Command("提问", description="检索 WeKnora 知识库并让麦麦作答", pattern=r"^/提问\s*(?P<query>.+)$")
    async def handle_ask_command(self, stream_id: str = "", **kwargs: Any) -> tuple[bool, str, bool]:
        """用户直接提问：检索片段，交给 MaiBot 的 planner 模型作答后发回聊天。"""

        question = self._extract_query(kwargs)
        if not question:
            return False, "用法：/提问 <你的问题>", True
        try:
            chunks = await self._retrieve(question)
        except WeKnoraError as exc:
            self.ctx.logger.error("知识库检索失败：%s", exc)
            await self.ctx.send.text(f"知识库检索失败：{exc}", stream_id)
            return False, f"知识库检索失败：{exc}", True
        if not chunks:
            await self.ctx.send.text(f"知识库里没有检索到与「{question}」相关的资料。", stream_id)
            return True, "知识库无命中", True

        fragments = self._format_chunks(question, chunks)
        result = await self.ctx.llm.generate(self._build_answer_prompt(question, fragments), task_name="planner")
        answer = str(result.get("response") or "").strip()
        if not result.get("success") or not answer:
            reason = str(result.get("error") or "").strip() or "模型没有返回内容"
            self.ctx.logger.error("知识库作答失败：%s", reason)
            await self.ctx.send.text(f"检索到 {len(chunks)} 条片段，但作答失败：{reason}", stream_id)
            return False, f"知识库作答失败：{reason}", True

        await self.ctx.send.text(answer, stream_id)
        return True, "已检索知识库并回复", True

    @HookHandler("maisaka.planner.before_request", name="weknora_bot_prompt", mode=HookMode.BLOCKING)
    async def inject_bot_prompt(self, **kwargs: Any) -> dict[str, Any]:
        """把配置的触发提示词作为系统消息追加到规划器上下文。"""

        try:
            prompt = self.config.prompt.bot_prompt.strip()
        except RuntimeError:
            return {"modified_kwargs": kwargs}
        if not prompt:
            return {"modified_kwargs": kwargs}
        items = kwargs.get("items")
        if not isinstance(items, list):
            return {"modified_kwargs": kwargs}
        modified = {**kwargs, "items": [*items, self._build_prompt_item(prompt)]}
        return {"modified_kwargs": modified}

    # ---------------------------------------------------------------- #
    # 检索流程
    # ---------------------------------------------------------------- #

    async def _retrieve(self, question: str) -> list[KnowledgeChunk]:
        """执行一次检索：解析知识库、核对向量模型、拉回片段。"""

        client = self._client or self._build_client()
        knowledge_bases = self._select_knowledge_bases(await client.list_knowledge_bases())
        await self._verify_embedding_model(client, knowledge_bases)
        options = SearchOptions(
            query=question,
            knowledge_base_ids=[str(knowledge_base.get("id") or "") for knowledge_base in knowledge_bases],
            match_count=self.config.retrieval.match_count,
        )
        return await client.search(options)

    def _build_client(self) -> WeKnoraClient:
        service = self.config.service
        return WeKnoraClient(
            base_url=service.url,
            api_key=service.api_key,
            timeout_seconds=float(service.timeout_seconds),
        )

    def _select_knowledge_bases(self, knowledge_bases: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """把配置里的知识库名称或 ID 解析成知识库对象；留空表示租户下全部知识库。"""

        if not knowledge_bases:
            raise WeKnoraError("WeKnora 中没有任何知识库")
        wanted = self.config.retrieval.knowledge_base.strip()
        if not wanted:
            return knowledge_bases

        target = wanted.lower()
        selected = [
            knowledge_base
            for knowledge_base in knowledge_bases
            if str(knowledge_base.get("id") or "") == wanted
            or str(knowledge_base.get("name") or "").strip().lower() == target
        ]
        if not selected:
            names = "、".join(
                f"{knowledge_base.get('name')}({knowledge_base.get('id')})" for knowledge_base in knowledge_bases
            )
            raise WeKnoraError(f"WeKnora 中没有名为「{wanted}」的知识库，可用知识库：{names}")
        return selected

    async def _verify_embedding_model(
        self, client: WeKnoraClient, knowledge_bases: list[dict[str, Any]]
    ) -> None:
        """核对知识库建库用的向量模型与 MaiBot embedding 任务配的是否同一个。"""

        if self._embedding_checked:
            return
        self._embedding_checked = True
        try:
            maibot_model = await asyncio.to_thread(resolve_embedding_model)
            models = await client.list_models()
        except (EmbeddingModelError, WeKnoraError) as exc:
            self.ctx.logger.warning("无法核对向量模型，本次跳过核对：%s", exc)
            return

        model_names = {str(model.get("id") or ""): str(model.get("name") or "") for model in models}
        for knowledge_base in knowledge_bases:
            weknora_name = model_names.get(str(knowledge_base.get("embedding_model_id") or ""), "")
            if weknora_name and self._is_same_model(weknora_name, maibot_model):
                self.ctx.logger.info("知识库「%s」向量模型与 MaiBot 一致：%s", knowledge_base.get("name"), weknora_name)
            else:
                self.ctx.logger.warning(
                    "知识库「%s」建库用的向量模型是「%s」，与 MaiBot embedding 任务配置的「%s」（%s）不一致，"
                    "召回质量可能受影响",
                    knowledge_base.get("name"),
                    weknora_name or "未知",
                    maibot_model.name,
                    maibot_model.identifier,
                )

    @staticmethod
    def _is_same_model(weknora_name: str, maibot_model: EmbeddingModel) -> bool:
        """WeKnora 的 Embedding 模型名与 MaiBot 的模型名或标识符是否指同一个模型。"""

        target = weknora_name.strip().lower()
        return target in (maibot_model.identifier.strip().lower(), maibot_model.name.strip().lower())

    def _format_chunks(self, question: str, chunks: list[KnowledgeChunk]) -> str:
        """把片段拼成给规划器看的文本。"""

        if not chunks:
            return f"知识库中没有检索到与「{question}」相关的片段。"
        limit = self.config.retrieval.max_chars_per_chunk
        blocks = [self._format_chunk(index, chunk, limit) for index, chunk in enumerate(chunks, start=1)]
        return (
            f"以下是 WeKnora 知识库中与「{question}」最相关的 {len(chunks)} 条原文片段。\n"
            "请只依据这些片段回答，片段里没有的信息如实说明资料未提及，不要编造。\n\n"
            + "\n\n".join(blocks)
        )

    @staticmethod
    def _format_chunk(index: int, chunk: KnowledgeChunk, limit: int) -> str:
        """拼出单条片段：来源、相关度与正文，超长正文按配置截断。"""

        source = chunk.title or chunk.filename or "未知文档"
        header = f"【片段 {index}】来源：{source} 第 {chunk.chunk_index} 段｜相关度 {chunk.score:.3f}"
        content = chunk.content.strip()
        if limit > 0 and len(content) > limit:
            content = f"{content[:limit].rstrip()}\n…（片段过长已截断，原文共 {len(content)} 字）"
        return f"{header}\n{content}"

    @staticmethod
    def _build_answer_prompt(question: str, fragments: str) -> str:
        """把片段包成给 MaiBot planner 模型的作答提示。"""

        return (
            f"{fragments}\n\n"
            f"用户问题：{question}\n"
            "请用中文回答这个问题：直接给结论和关键数据，只依据上面的片段，"
            "片段里没有的内容如实说明资料未提及，不要编造。"
        )

    @staticmethod
    def _extract_query(kwargs: dict[str, Any]) -> str:
        """从命令调用参数里取出问题文本。"""

        matched_groups = kwargs.get("matched_groups")
        if isinstance(matched_groups, dict):
            query = str(matched_groups.get("query") or "").strip()
            if query:
                return query
        text = str(kwargs.get("text") or "").strip()
        if text.startswith("/提问"):
            return text[len("/提问") :].strip()
        return ""

    @staticmethod
    def _build_prompt_item(prompt: str) -> dict[str, Any]:
        """构造注入规划器上下文的系统消息项。"""

        return {
            "item_type": "SystemMessageItem",
            "meta": {
                "item_id": f"weknora-bot-prompt-{uuid.uuid4().hex}",
                "logical_turn_id": None,
                "timestamp": datetime.now().isoformat(timespec="microseconds"),
            },
            "parts": [{"type": "text", "text": prompt}],
        }


def create_plugin() -> WeKnoraQaPlugin:
    """创建 WeKnora 检索插件实例。"""

    return WeKnoraQaPlugin()
