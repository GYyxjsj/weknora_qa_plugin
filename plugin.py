"""WeKnora 知识库问答插件。

给麦麦一条通往自建 WeKnora 知识库的路：规划器可以调用 ``weknora_ask`` 工具，
用户也可以直接发 ``/提问 <问题>``。问答在 WeKnora 侧完成，模型默认用 MaiBot 自身
planner 任务所选模型的无思考版本——解析主程序 ``config/model_config.toml`` 拿到
planner 的 model_identifier，在 WeKnora 里按同名模型下发，并强制关闭思考。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

import asyncio
import uuid

from maibot_sdk import Command, Field, HookHandler, MaiBotPlugin, PluginConfigBase, Tool
from maibot_sdk.types import HookMode, ToolParameterInfo, ToolParamType

from .planner_model import PlannerModelError, describe_models, resolve_planner_model
from .weknora_client import AskOptions, WeKnoraClient, WeKnoraError

_DEFAULT_SESSION_KEY = "__default__"


class PluginSectionConfig(PluginConfigBase):
    """插件基础配置。"""

    __ui_label__ = "插件"
    __ui_icon__ = "cog"
    __ui_order__ = 0

    enabled: bool = Field(default=False, description="是否启用插件", json_schema_extra={"label": "启用"})
    config_version: str = Field(default="1.0.0", description="配置版本", json_schema_extra={"label": "配置版本"})


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
        description="WeKnora API Key（请求头 X-API-Key），智能体问答需要 full_access 权限的密钥",
        json_schema_extra={"label": "API Key"},
    )
    agent: str = Field(
        default="",
        description="智能体 ID 或名称；留空则使用 WeKnora 内置的快速问答",
        json_schema_extra={"label": "智能体"},
    )
    timeout_seconds: int = Field(
        default=180,
        ge=10,
        le=1800,
        description="单次问答超时（秒）",
        json_schema_extra={"label": "超时（秒）"},
    )


class ModelConfig(PluginConfigBase):
    """问答所用模型配置。"""

    __ui_label__ = "模型"
    __ui_icon__ = "brain"
    __ui_order__ = 2

    model: str = Field(
        default="",
        description="WeKnora 模型名称或 ID；留空则自动使用 MaiBot planner 模型",
        json_schema_extra={"label": "模型"},
    )
    reasoning_effort: str = Field(
        default="off",
        description="思考强度：off=无思考，auto=自动，其余为具体档位",
        json_schema_extra={"label": "思考强度"},
    )


class PromptConfig(PluginConfigBase):
    """提示词配置。"""

    __ui_label__ = "提示词"
    __ui_icon__ = "message-circle"
    __ui_order__ = 3

    bot_prompt: str = Field(
        default="",
        description="注入麦麦规划器上下文的提示词，留空则不注入",
        json_schema_extra={"label": "Bot 提示词"},
    )
    agent_prompt: str = Field(
        default="",
        description="拼在问题前面发给 WeKnora 智能体的前置提示词",
        json_schema_extra={"label": "智能体提示词"},
    )


class WeKnoraQaConfig(PluginConfigBase):
    """WeKnora 问答插件配置。"""

    plugin: PluginSectionConfig = Field(default_factory=PluginSectionConfig)
    service: ServiceConfig = Field(default_factory=ServiceConfig)
    model: ModelConfig = Field(default_factory=ModelConfig)
    prompt: PromptConfig = Field(default_factory=PromptConfig)


class WeKnoraQaPlugin(MaiBotPlugin):
    """WeKnora 知识库问答插件。"""

    config_model = WeKnoraQaConfig

    def __init__(self) -> None:
        super().__init__()
        self._client: WeKnoraClient | None = None
        # 同一个聊天流复用同一个 WeKnora 会话，问答才有上下文。
        self._stream_sessions: dict[str, str] = {}
        self._agent_ids: dict[str, str] = {}
        self._model_ids: dict[str, tuple[str, str]] = {}

    # ---------------------------------------------------------------- #
    # 生命周期
    # ---------------------------------------------------------------- #

    async def on_load(self) -> None:
        self._client = self._build_client()
        self.ctx.logger.info(
            "WeKnora 问答插件已加载：url=%s，agent=%s",
            self.config.service.url,
            self.config.service.agent or "自动",
        )

    async def on_unload(self) -> None:
        self._client = None
        self._stream_sessions.clear()
        self.ctx.logger.info("WeKnora 问答插件已卸载")

    async def on_config_update(self, scope: str, config_data: dict[str, Any], version: str) -> None:
        """配置热更新：重建客户端并清空缓存，让新参数立即生效。"""

        del config_data
        if scope != "self":
            self.ctx.logger.debug("忽略非插件自身配置更新：scope=%s version=%s", scope, version)
            return
        self._client = self._build_client()
        self._stream_sessions.clear()
        self._agent_ids.clear()
        self._model_ids.clear()
        self.ctx.logger.info("配置已热更新（版本 %s），已重建客户端", version)

    # ---------------------------------------------------------------- #
    # 组件声明
    # ---------------------------------------------------------------- #

    def get_components(self) -> list[dict[str, Any]]:
        """给问答工具放宽 RPC 超时：一次 RAG 问答经常超过默认的 60 秒。"""

        components = super().get_components()
        try:
            timeout_ms = (self.config.service.timeout_seconds + 30) * 1000
        except RuntimeError:
            return components
        for component in components:
            metadata = component.get("metadata")
            if component.get("name") == "weknora_ask" and isinstance(metadata, dict):
                metadata["timeout_ms"] = timeout_ms
        return components

    @Tool(
        "weknora_ask",
        description="向 WeKnora 知识库智能体提问并取回答案。遇到需要查证的设定、剧情、关卡、机制、术语等问题时使用。",
        parameters=[
            ToolParameterInfo(
                name="query",
                param_type=ToolParamType.STRING,
                description="要查询的问题，写成完整问句",
                required=True,
            ),
        ],
    )
    async def handle_weknora_ask(self, query: str = "", stream_id: str = "", **kwargs: Any) -> dict[str, str]:
        """把问题转给 WeKnora 智能体，把答案交回规划器。"""

        del kwargs
        question = (query or "").strip()
        if not question:
            return {"name": "weknora_ask", "content": "问题为空，无法查询知识库。"}
        try:
            answer = await self._ask(question, stream_id)
        except (WeKnoraError, PlannerModelError) as exc:
            self.ctx.logger.error("知识库问答失败：%s", exc)
            return {"name": "weknora_ask", "content": f"知识库问答失败：{exc}"}
        return {"name": "weknora_ask", "content": answer}

    @Command("提问", description="向 WeKnora 知识库提问", pattern=r"^/提问\s*(?P<query>.+)$")
    async def handle_ask_command(self, stream_id: str = "", **kwargs: Any) -> tuple[bool, str, bool]:
        """用户直接提问，答案直接发回聊天。"""

        question = self._extract_query(kwargs)
        if not question:
            return False, "用法：/提问 <你的问题>", True
        try:
            answer = await self._ask(question, stream_id)
        except (WeKnoraError, PlannerModelError) as exc:
            self.ctx.logger.error("知识库问答失败：%s", exc)
            await self.ctx.send.text(f"知识库问答失败：{exc}", stream_id)
            return False, f"知识库问答失败：{exc}", True
        await self.ctx.send.text(answer, stream_id)
        return True, "已向知识库提问并回复", True

    @HookHandler("maisaka.planner.before_request", name="weknora_bot_prompt", mode=HookMode.BLOCKING)
    async def inject_bot_prompt(self, **kwargs: Any) -> dict[str, Any]:
        """把配置的提示词作为系统消息追加到规划器上下文。"""

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
    # 问答流程
    # ---------------------------------------------------------------- #

    async def _ask(self, question: str, stream_id: str) -> str:
        """执行一次问答：解析智能体与模型，取会话，发请求。"""

        client = self._client or self._build_client()
        agent_id = await self._resolve_agent_id(client)
        model_id, reasoning_effort = await self._resolve_model(client)
        session_id = await self._ensure_session(client, stream_id)
        options = AskOptions(
            query=self._apply_agent_prompt(question),
            agent_id=agent_id,
            summary_model_id=model_id,
            reasoning_effort=reasoning_effort,
            web_search_enabled=True,
            session_id=session_id,
        )
        return await client.ask(options)

    def _build_client(self) -> WeKnoraClient:
        service = self.config.service
        return WeKnoraClient(
            base_url=service.url,
            api_key=service.api_key,
            timeout_seconds=float(service.timeout_seconds),
        )

    async def _resolve_agent_id(self, client: WeKnoraClient) -> str:
        """把配置里的智能体名称或 ID 解析成 WeKnora 的智能体 ID。"""

        wanted = self.config.service.agent.strip()
        cached = self._agent_ids.get(wanted)
        if cached:
            return cached

        agents = await client.list_agents()
        resolved = ""
        if wanted:
            for agent in agents:
                if str(agent.get("id") or "") == wanted:
                    resolved = wanted
                    break
                if str(agent.get("name") or "").strip().lower() == wanted.lower():
                    resolved = str(agent.get("id") or "")
                    break
            if not resolved:
                names = "、".join(f"{agent.get('name')}({agent.get('id')})" for agent in agents)
                raise WeKnoraError(f"WeKnora 中没有名为「{wanted}」的智能体，可用智能体：{names or '无'}")
        else:
            fallback = next((agent for agent in agents if agent.get("is_builtin")), None)
            if fallback is None and agents:
                fallback = agents[0]
            if fallback is None:
                raise WeKnoraError("WeKnora 中没有可用智能体")
            resolved = str(fallback.get("id") or "")

        self._agent_ids[wanted] = resolved
        return resolved

    async def _resolve_model(self, client: WeKnoraClient) -> tuple[str, str]:
        """解析问答模型：配置指定优先，否则用 MaiBot planner 模型。返回 (模型 ID, 思考强度)。"""

        configured = self.config.model.model.strip()
        if configured:
            cached = self._model_ids.get(configured)
            if cached:
                return cached
            models = await client.list_models()
            model = self._match_model(models, configured)
            if model is None:
                raise WeKnoraError(f"WeKnora 中没有名为「{configured}」的模型，可用模型：{describe_models(models)}")
            resolved = (str(model.get("id") or ""), self._resolve_reasoning_effort(model))
            self._model_ids[configured] = resolved
            return resolved

        # 跟随主程序 planner：主程序换了模型，这里下次提问就用新的。
        models = await client.list_models()
        planner = await asyncio.to_thread(resolve_planner_model)
        model = self._match_model(models, planner.identifier)
        if model is None:
            raise WeKnoraError(
                f"MaiBot planner 模型「{planner.name}」（{planner.identifier}）在 WeKnora 中不存在，"
                f"可用模型：{describe_models(models)}"
            )
        self.ctx.logger.info("使用 MaiBot planner 模型：%s（%s）", planner.name, planner.identifier)
        return str(model.get("id") or ""), self._resolve_reasoning_effort(model)

    @staticmethod
    def _match_model(models: list[dict[str, Any]], wanted: str) -> dict[str, Any] | None:
        """按 ID 或名称匹配模型，大小写不敏感。"""

        target = wanted.strip().lower()
        for model in models:
            if str(model.get("id") or "").strip().lower() == target:
                return model
        for model in models:
            if str(model.get("name") or "").strip().lower() == target:
                return model
        return None

    def _resolve_reasoning_effort(self, model: dict[str, Any]) -> str:
        """按模型能力决定是否下发思考强度；模型不支持时不下发该参数。"""

        configured = self.config.model.reasoning_effort.strip() or "off"
        capabilities = model.get("capabilities") or {}
        levels = capabilities.get("thinking_levels") or []
        if not levels:
            return ""
        if configured not in levels:
            self.ctx.logger.warning(
                "模型 %s 不支持思考强度「%s」（可选：%s），本次不下发该参数",
                model.get("name"),
                configured,
                "、".join(str(level) for level in levels),
            )
            return ""
        return configured

    async def _ensure_session(self, client: WeKnoraClient, stream_id: str) -> str:
        """按聊天流复用 WeKnora 会话；会话失效时重建。"""

        key = stream_id or _DEFAULT_SESSION_KEY
        session_id = self._stream_sessions.get(key)
        if session_id:
            return session_id
        session_id = await client.create_session()
        self._stream_sessions[key] = session_id
        return session_id

    def _apply_agent_prompt(self, question: str) -> str:
        """把智能体提示词拼在问题前面。"""

        prefix = self.config.prompt.agent_prompt.strip()
        if not prefix:
            return question
        return f"{prefix}\n\n{question}"

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
    """创建 WeKnora 问答插件实例。"""

    return WeKnoraQaPlugin()
