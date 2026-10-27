"""MaiBot planner 模型解析。

插件默认要「用自己的 planner 模型」在 WeKnora 侧问答，而 MaiBot 的模型表只存在于
主程序的 ``config/model_config.toml``，插件拿不到主程序的模型对象，只能自己读这份
配置：取 ``model_task_config.planner.model_list`` 的首个模型，再从 ``[[models]]``
里找到同名条目，读出它的 ``model_identifier``——那正是上游 API 认的模型名。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import os
import tomllib

_MODEL_CONFIG_ENV = "MAIBOT_MODEL_CONFIG"
_MODEL_CONFIG_RELATIVE = Path("config") / "model_config.toml"


class PlannerModelError(RuntimeError):
    """无法从主程序配置里解析出 planner 模型。"""


@dataclass(frozen=True, slots=True)
class PlannerModel:
    """planner 任务选中的模型。"""

    name: str
    identifier: str
    enable_thinking: bool


def find_model_config_path() -> Path | None:
    """定位主程序的 model_config.toml。

    先看环境变量 ``MAIBOT_MODEL_CONFIG``，否则从插件目录逐级向上找
    ``config/model_config.toml``（插件位于 ``<MaiBot>/plugins/<插件名>/``）。
    """

    override = os.environ.get(_MODEL_CONFIG_ENV, "").strip()
    if override:
        override_path = Path(override)
        return override_path if override_path.is_file() else None

    for parent in Path(__file__).resolve().parents:
        candidate = parent / _MODEL_CONFIG_RELATIVE
        if candidate.is_file():
            return candidate
    return None


def resolve_planner_model() -> PlannerModel:
    """解析 planner 任务当前使用的模型。

    Raises:
        PlannerModelError: 找不到配置文件、planner 未配置模型或模型定义缺失。
    """

    config_path = find_model_config_path()
    if config_path is None:
        raise PlannerModelError(
            f"找不到主程序的 model_config.toml，可用环境变量 {_MODEL_CONFIG_ENV} 指定路径"
        )

    with config_path.open("rb") as config_file:
        data = tomllib.load(config_file)

    planner = (data.get("model_task_config") or {}).get("planner") or {}
    model_list = planner.get("model_list") or []
    if not model_list:
        raise PlannerModelError(f"{config_path} 中 model_task_config.planner.model_list 为空")

    model_name = str(model_list[0]).strip()
    for model in data.get("models") or []:
        if str(model.get("name") or "").strip() != model_name:
            continue
        identifier = str(model.get("model_identifier") or "").strip()
        if not identifier:
            break
        extra_params = model.get("extra_params") or {}
        return PlannerModel(
            name=model_name,
            identifier=identifier,
            enable_thinking=bool(extra_params.get("enable_thinking")),
        )

    raise PlannerModelError(f"{config_path} 中找不到名为「{model_name}」的模型定义")


def describe_models(models: list[dict[str, Any]]) -> str:
    """把模型列表拼成便于阅读的一行，用于报错提示。"""

    names = [str(model.get("name") or model.get("id") or "") for model in models]
    return "、".join(name for name in names if name) or "无"
