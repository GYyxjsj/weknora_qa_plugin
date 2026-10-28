"""MaiBot 向量模型解析。

检索用的向量模型必须和 WeKnora 建库时用的是同一个，否则两边向量空间对不上，召回
质量会无声地掉。而 MaiBot 的模型表只存在于主程序的 ``config/model_config.toml``，
插件拿不到主程序的模型对象，只能自己读这份配置：取 ``model_task_config.embedding``
的 ``model_list`` 首个模型，再从 ``[[models]]`` 里找到同名条目，读出它的
``model_identifier``——那正是 WeKnora 里 Embedding 模型的名字。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import os
import tomllib

_MODEL_CONFIG_ENV = "MAIBOT_MODEL_CONFIG"
_MODEL_CONFIG_RELATIVE = Path("config") / "model_config.toml"
_EMBEDDING_TASK = "embedding"


class EmbeddingModelError(RuntimeError):
    """无法从主程序配置里解析出向量模型。"""


@dataclass(frozen=True, slots=True)
class EmbeddingModel:
    """embedding 任务选中的模型。"""

    name: str
    identifier: str


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


def resolve_embedding_model() -> EmbeddingModel:
    """解析 embedding 任务当前使用的模型。

    Raises:
        EmbeddingModelError: 找不到配置文件、embedding 任务未配置模型或模型定义缺失。
    """

    config_path = find_model_config_path()
    if config_path is None:
        raise EmbeddingModelError(
            f"找不到主程序的 model_config.toml，可用环境变量 {_MODEL_CONFIG_ENV} 指定路径"
        )

    with config_path.open("rb") as config_file:
        data = tomllib.load(config_file)

    embedding = (data.get("model_task_config") or {}).get(_EMBEDDING_TASK) or {}
    model_list = embedding.get("model_list") or []
    if not model_list:
        raise EmbeddingModelError(f"{config_path} 中 model_task_config.embedding.model_list 为空")

    model_name = str(model_list[0]).strip()
    for model in data.get("models") or []:
        if str(model.get("name") or "").strip() != model_name:
            continue
        identifier = str(model.get("model_identifier") or "").strip()
        if not identifier:
            break
        return EmbeddingModel(name=model_name, identifier=identifier)

    raise EmbeddingModelError(f"{config_path} 中找不到名为「{model_name}」的模型定义")
