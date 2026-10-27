# 更新日志

本项目遵循语义化版本（SemVer）。

## v1.0.0 (2026-10-27)

### 新增

- **WeKnora 知识库问答插件**：提供 `weknora_ask` 工具给规划器调用，以及 `/提问` 指令给用户直接提问。
- **可配置服务地址与智能体**：`config.toml` 的 `[service]` 段配置 WeKnora 地址、API Key、智能体与超时。
- **模型默认使用 MaiBot planner 模型的无思考版本**：`[model].model` 留空时自动解析主程序
  `config/model_config.toml` 的 planner 模型标识，在 WeKnora 侧按同名模型下发，并关闭思考。
- **可配置提示词**：`[prompt].bot_prompt` 注入麦麦规划器上下文，`[prompt].agent_prompt`
  拼在问题前面发给 WeKnora 智能体。
