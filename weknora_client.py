"""WeKnora 知识库检索客户端。

只做检索，不做问答：``POST /api/v1/knowledge-search`` 用知识库建库时绑定的向量模型
把查询向量化，召回原文片段并 rerank 后原样返回，全程不经过任何 LLM 总结，答案由麦麦
自己思考。另外提供列知识库、列模型两个只读接口，用于把配置里的知识库名称解析成 ID，
以及核对 WeKnora 侧的向量模型和 MaiBot 配的是不是同一个。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import httpx


class WeKnoraError(RuntimeError):
    """WeKnora 请求失败或响应无法解析。"""


@dataclass(slots=True)
class KnowledgeChunk:
    """一条检索命中的知识库片段。"""

    content: str
    title: str
    filename: str
    score: float
    chunk_index: int


@dataclass(slots=True)
class SearchOptions:
    """单次检索参数。"""

    query: str
    knowledge_base_ids: list[str]
    match_count: int = 0


class WeKnoraClient:
    """WeKnora REST 客户端。"""

    def __init__(self, base_url: str, api_key: str, timeout_seconds: float) -> None:
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key.strip()
        self._timeout = timeout_seconds

    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self._api_key:
            headers["X-API-Key"] = self._api_key
        return headers

    async def _get_list(self, path: str) -> list[dict[str, Any]]:
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            response = await client.get(f"{self._base_url}{path}", headers=self._headers())
        if response.status_code >= 400:
            raise WeKnoraError(f"GET {path} 失败 HTTP {response.status_code}：{_describe_error(response)}")
        payload = response.json()
        data = payload.get("data")
        if not isinstance(data, list):
            raise WeKnoraError(f"GET {path} 返回结构异常：缺少 data 列表")
        return data

    async def list_knowledge_bases(self) -> list[dict[str, Any]]:
        """列出当前租户可见的知识库。"""

        return await self._get_list("/api/v1/knowledge-bases")

    async def list_models(self) -> list[dict[str, Any]]:
        """列出当前租户可用的模型。"""

        return await self._get_list("/api/v1/models")

    async def search(self, options: SearchOptions) -> list[KnowledgeChunk]:
        """检索知识库原文片段，WeKnora 不做 LLM 总结。"""

        body: dict[str, Any] = {
            "query": options.query,
            "knowledge_base_ids": options.knowledge_base_ids,
        }
        # match_count 为 0 时不下发，沿用租户检索配置里的默认条数。
        if options.match_count > 0:
            body["match_count"] = options.match_count

        async with httpx.AsyncClient(timeout=self._timeout) as client:
            response = await client.post(
                f"{self._base_url}/api/v1/knowledge-search",
                headers=self._headers(),
                json=body,
            )
        if response.status_code >= 400:
            raise WeKnoraError(f"知识库检索失败 HTTP {response.status_code}：{_describe_error(response)}")

        payload = response.json()
        data = payload.get("data")
        if not isinstance(data, list):
            raise WeKnoraError("知识库检索返回结构异常：缺少 data 列表")
        return [_parse_chunk(item) for item in data]


def _parse_chunk(item: Any) -> KnowledgeChunk:
    """把一条检索结果转成片段对象。"""

    if not isinstance(item, dict):
        raise WeKnoraError(f"知识库检索返回了非对象的结果项：{type(item).__name__}")
    return KnowledgeChunk(
        content=str(item.get("content") or ""),
        title=str(item.get("knowledge_title") or ""),
        filename=str(item.get("knowledge_filename") or ""),
        score=float(item.get("score") or 0.0),
        chunk_index=int(item.get("chunk_index") or 0),
    )


def _describe_error(response: httpx.Response) -> str:
    """取出 WeKnora 错误响应里的 message，取不到就退回原始响应体。"""

    try:
        payload = response.json()
    except ValueError:
        return response.text[:300]
    error = payload.get("error") if isinstance(payload, dict) else None
    if isinstance(error, dict):
        message = str(error.get("message") or "").strip()
        if message:
            return message
    return str(payload)[:300]
