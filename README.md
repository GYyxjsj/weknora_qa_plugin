# WeKnora 知识库问答插件

把麦麦的问题转发到自建 [WeKnora](https://github.com/Tencent/WeKnora) 知识库：规划器可以调用
`weknora_ask` 工具，用户也可以直接发 `/提问 <问题>`，答案由 WeKnora 智能体（RAG）产出。

## 能力

| 入口 | 说明 |
| --- | --- |
| 工具 `weknora_ask` | 规划器调用，参数 `query`（完整问句），返回智能体回答 |
| 指令 `/提问` | 用户直接提问，答案直接发回聊天，例：`/提问 第四关的 boss 叫什么` |

同一个聊天流会复用同一个 WeKnora 会话，因此可以顺着上一句继续追问——但能否带上上下文还取决于
智能体自身是否开启「多轮对话」（`multi_turn_enabled`），WeKnora 内置智能体默认开启，自建智能体需要在
智能体配置里自行打开。

## 配置

配置文件是插件目录下的 `config.toml`（含 API Key，已被 `.gitignore` 忽略；插件目录不存在时宿主会按默认值生成）。

```toml
[plugin]
enabled = false
config_version = "1.0.0"

[service]
url = "http://127.0.0.1:8080"   # WeKnora 服务地址
api_key = ""                     # WeKnora API Key（请求头 X-API-Key），智能体问答需要 full_access 权限的密钥
agent = ""                       # 智能体 ID 或名称；留空则用 WeKnora 内置的快速问答
timeout_seconds = 180            # 单次问答超时（秒）

[model]
model = ""                       # WeKnora 模型名称或 ID；留空则自动使用 MaiBot planner 模型
reasoning_effort = "off"         # off=无思考，auto=自动，其余为具体档位

[prompt]
bot_prompt = ""                  # 注入麦麦规划器上下文的提示词（留空则不注入）
agent_prompt = ""                # 拼在问题前面发给 WeKnora 智能体的前置提示词
```

### 模型默认值：MaiBot planner 模型的无思考版本

`model` 留空时，插件会读主程序的 `config/model_config.toml`，取出
`model_task_config.planner.model_list` 的首个模型，再从 `[[models]]` 里找到同名条目，
读出它的 `model_identifier`——那正是上游 API 认的模型名。插件再到 WeKnora 里按同名模型下发
`summary_model_id`，并配合 `reasoning_effort = "off"` 关闭思考。

也就是说：主程序的 planner 换成什么模型，插件就用什么模型来回答；两个模型需要在 WeKnora
侧同名存在。若 WeKnora 里没有同名模型，插件会直接报错并列出可用模型，而不是悄悄换用别的模型。

配置文件不在主程序目录下时，可以用环境变量 `MAIBOT_MODEL_CONFIG` 指定 `model_config.toml` 的路径。

## 提示词

两处提示词互不影响：

- `bot_prompt`：注入麦麦规划器上下文（`maisaka.planner.before_request` 钩子），用于告诉麦麦
  什么时候该去查知识库。
- `agent_prompt`：拼在问题前面发给 WeKnora 智能体，用于约束 RAG 一侧的提问方式。

## 常见问题

**问答报 `connectex: No connection could be made`**
WeKnora 侧模型不可达。插件默认会用 MaiBot planner 模型覆盖智能体自带的模型，如果仍然失败，
去 WeKnora 的模型设置里确认该模型的上游地址可用。

**问答报「MaiBot planner 模型在 WeKnora 中不存在」**
主程序 planner 用的模型在 WeKnora 侧还没有登记。在 WeKnora 里加一个同名模型，或者在
`config.toml` 的 `model` 里直接写 WeKnora 侧已有的模型名。

**工具调用超时**
RAG 问答偏慢，插件已把工具的 RPC 超时放宽到 `timeout_seconds + 30` 秒；上游慢时把
`timeout_seconds` 调大即可。
