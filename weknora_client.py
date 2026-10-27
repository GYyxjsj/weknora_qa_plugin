"""WeKnora 服务端问答客户端。

封装三件事：列智能体、列模型、发起问答。问答走 ``/api/v1/agent-chat/{session_id}``，
WeKnora 以 SSE 分片返回，其中 ``response_type="answer"`` 的分片拼成最终答案。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, AsyncIterator

import httpx
import json


class WeKnoraError(RuntimeError):
    """WeKnora 请求失败或响应无法解析。"""


@dataclass(slots=True)
class AskOptions:
    """单次问答参数。"""

    query: str
    agent_id: str = ""
    summary_model_id: str = ""
    reasoning_effort: str = ""
    web_search_enabled: bool = True
    session_id: str = ""


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
            raise WeKnoraError(f"GET {path} 失败 HTTP {response.status_code}：{response.text[:300]}")
        payload = response.json()
        data = payload.get("data")
        if not isinstance(data, list):
            raise WeKnoraError(f"GET {path} 返回结构异常：缺少 data 列表")
        return data

    async def list_agents(self) -> list[dict[str, Any]]:
        """列出当前租户可用的智能体。"""

        return await self._get_list("/api/v1/agents")

    async def list_models(self) -> list[dict[str, Any]]:
        """列出当前租户可用的模型。"""

        return await self._get_list("/api/v1/models")

    async def create_session(self, title: str = "MaiBot 知识库问答") -> str:
        """创建会话，返回会话 ID。"""

        async with httpx.AsyncClient(timeout=self._timeout) as client:
            response = await client.post(
                f"{self._base_url}/api/v1/sessions",
                headers=self._headers(),
                json={"title": title},
            )
        if response.status_code >= 400:
            raise WeKnoraError(f"创建会话失败 HTTP {response.status_code}：{response.text[:300]}")
        session_id = str((response.json().get("data") or {}).get("id") or "")
        if not session_id:
            raise WeKnoraError("创建会话失败：响应缺少 data.id")
        return session_id

    async def ask(self, options: AskOptions) -> str:
        """发起一次问答，返回智能体的完整回答。"""

        body: dict[str, Any] = {
            "query": options.query,
            "agent_enabled": True,
            "agent_id": options.agent_id,
            "summary_model_id": options.summary_model_id,
            "reasoning_effort": options.reasoning_effort,
            "web_search_enabled": options.web_search_enabled,
            "channel": "api",
        }
        url = f"{self._base_url}/api/v1/agent-chat/{options.session_id}"
        answer_parts: list[str] = []
        stream_error = ""

        async with httpx.AsyncClient(timeout=self._timeout) as client:
            async with client.stream("POST", url, headers=self._headers(), json=body) as response:
                if response.status_code >= 400:
                    detail = (await response.aread()).decode("utf-8", "ignore")
                    raise WeKnoraError(f"问答请求失败 HTTP {response.status_code}：{detail[:500]}")
                async for event in _iter_sse_events(response):
                    response_type = str(event.get("response_type") or "")
                    content = event.get("content")
                    if response_type == "answer" and isinstance(content, str):
                        answer_parts.append(content)
                    elif response_type == "error":
                        stream_error = str(content or "WeKnora 返回未知错误")

        if stream_error:
            raise WeKnoraError(f"问答失败：{stream_error}")
        answer = "".join(answer_parts).strip()
        if not answer:
            raise WeKnoraError("问答结束但没有拿到回答内容")
        return answer


async def _iter_sse_events(response: httpx.Response) -> AsyncIterator[dict[str, Any]]:
    """把 SSE 响应拆成一个个事件字典。"""

    data_lines: list[str] = []
    async for raw_line in response.aiter_lines():
        line = raw_line.strip()
        if not line:
            event = _parse_sse_payload(data_lines)
            data_lines = []
            if event is not None:
                yield event
            continue
        if line.startswith("data:"):
            data_lines.append(line[5:].strip())
    event = _parse_sse_payload(data_lines)
    if event is not None:
        yield event


def _parse_sse_payload(data_lines: list[str]) -> dict[str, Any] | None:
    """把一个事件的多行 data 解析成字典，解析失败返回 None。"""

    if not data_lines:
        return None
    try:
        payload = json.loads("\n".join(data_lines))
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, dict):
        return None
    return payload
