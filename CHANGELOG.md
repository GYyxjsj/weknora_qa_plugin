# 更新日志

本项目遵循语义化版本（SemVer）。

## v1.1.0 (2026-10-28)

### 主要功能

- **改为纯检索**：插件不再让 WeKnora 生成答案，只调 `POST /api/v1/knowledge-search` 把知识库
  原文片段拉回来，交给麦麦自己的规划器思考作答。
- **工具改名**：`weknora_ask` → `weknora_search`，返回若干条带来源与相关度的原文片段。
- **`/提问` 走 MaiBot 自己的模型**：检索到片段后调 `llm.generate(task_name="planner")` 组织答案，
  不再依赖 WeKnora 侧的问答模型。
- **向量模型核对**：加载后首次检索时比对每个知识库建库用的 Embedding 模型与 MaiBot
  `embedding` 任务配置的模型，不一致时告警。

### 细节

- 移除智能体相关的一切：`[service].agent`、`agent-chat` SSE 通道、会话创建与复用、
  `[model]` 段（WeKnora 侧模型与思考强度）、`[prompt].agent_prompt`。
- 新增 `[retrieval]` 段：`knowledge_base`（名称或 ID，留空为全部知识库）、`match_count`、
  `max_chars_per_chunk`。
- 片段过长时按 `max_chars_per_chunk` 截断并标注原文字数，避免撑爆规划器上下文。
- manifest 能力声明补上 `llm.generate`；检索超时默认降到 60 秒（不再有 LLM 生成环节）。
- `planner_model.py` 改名为 `embedding_model.py`，解析对象从 planner 模型换成 embedding 模型。

## v1.0.0 (2026-10-27)

### 新增

- **WeKnora 知识库问答插件**：提供 `weknora_ask` 工具给规划器调用，以及 `/提问` 指令给用户直接提问。
- **可配置服务地址与智能体**：`config.toml` 的 `[service]` 段配置 WeKnora 地址、API Key、智能体与超时。
- **模型默认使用 MaiBot planner 模型的无思考版本**：`[model].model` 留空时自动解析主程序
  `config/model_config.toml` 的 planner 模型标识，在 WeKnora 侧按同名模型下发，并关闭思考。
- **可配置提示词**：`[prompt].bot_prompt` 注入麦麦规划器上下文，`[prompt].agent_prompt`
  拼在问题前面发给 WeKnora 智能体。
