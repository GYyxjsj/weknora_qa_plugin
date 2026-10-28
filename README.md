# WeKnora 知识库检索插件

从自建 [WeKnora](https://github.com/Tencent/WeKnora) 知识库把**原文片段**拉回来，交给麦麦自己思考。
插件不让 WeKnora 生成答案——只调 `POST /api/v1/knowledge-search` 做向量召回 + rerank，
返回若干条带来源与相关度的段落，由麦麦的规划器组织回答。

## 能力

| 入口 | 说明 |
| --- | --- |
| 工具 `weknora_search` | 规划器调用，参数 `query`（检索语句），返回原文片段，答案由规划器自己写 |
| 指令 `/提问` | 用户直接提问，例：`/提问 萃香的四费卡有哪些`；插件检索片段后交给 MaiBot 的 planner 模型作答，答案发回聊天 |

检索是无状态的：不建会话、不带历史，每次都是独立的一次向量召回。追问时规划器自己决定要不要再查一次。

## 配置

配置文件是插件目录下的 `config.toml`（含 API Key，已被 `.gitignore` 忽略；文件不存在时宿主会按默认值生成）。

```toml
[plugin]
enabled = false
config_version = "1.1.0"

[service]
url = "http://127.0.0.1:8080"   # WeKnora 服务地址
api_key = ""                     # WeKnora API Key（请求头 X-API-Key），需要能访问目标知识库
timeout_seconds = 60             # 单次检索超时（秒）

[retrieval]
knowledge_base = ""              # 知识库名称或 ID；留空则检索租户下的全部知识库
match_count = 3                  # 取回的片段数量，越多上下文越长
max_chars_per_chunk = 2000       # 单条片段最多注入多少字符，0 表示不截断

[prompt]
bot_prompt = ""                  # 注入麦麦规划器上下文的提示词，说明什么时候该查知识库
```

## 向量模型

召回用的向量模型不是插件选的，而是**知识库建库时绑定的那一个**（WeKnora 服务端做 embedding，
HTTP 接口只收查询文本，不收向量）。所以插件做的是核对：读主程序 `config/model_config.toml` 里
`model_task_config.embedding.model_list` 的首个模型及其 `model_identifier`，再和每个知识库的
`embedding_model_id` 对应的 WeKnora 模型名比对，一致就记一条 info，不一致就告警——两边向量空间
对不上时召回质量会无声地掉，值得在日志里留一行。

核对只在加载后第一次检索时做一次，配置热更新后会重新核对。配置文件不在主程序目录下时，
可以用环境变量 `MAIBOT_MODEL_CONFIG` 指定 `model_config.toml` 的路径。

## 提示词

只有 `bot_prompt` 一处，通过 `maisaka.planner.before_request` 钩子作为系统消息追加到规划器上下文，
用来告诉麦麦什么时候该调 `weknora_search`。留空则不注入。

工具描述本身已经写了适用场景，`bot_prompt` 是用来补充你自己那份知识库的边界的，例如：

```
遇到《东方非想天则》的角色、符卡、帧数、连段、机制、术语类问题时，先用 weknora_search 查资料再回答；
查询语句写清角色名和具体招式。片段里没有的数据就说资料未提及，不要凭印象报帧数。
```

## 返回给规划器的内容

```
以下是 WeKnora 知识库中与「<query>」最相关的 3 条原文片段。
请只依据这些片段回答，片段里没有的信息如实说明资料未提及，不要编造。

【片段 1】来源：Suika_Ibuki.txt 第 8 段｜相关度 0.986
<原文，超过 max_chars_per_chunk 时截断并标注原文字数>
```

没有命中时返回「知识库中没有检索到与「<query>」相关的片段」，规划器据此如实回答。

## 常见问题

**检索报 `knowledge base not found`**
`knowledge_base` 写的名称或 ID 在 WeKnora 里不存在。插件会先列知识库再解析，名字写错时
报错里会带上全部可用知识库；留空即检索全部。

**检索报 `connectex: No connection could be made`**
WeKnora 服务没起，或 `url` 写错。检索不经过任何 LLM 上游，因此 WeKnora 侧的问答模型
是否可达都不影响这个插件。

**工具调用超时**
插件已把工具的 RPC 超时放宽到 `timeout_seconds + 30` 秒。向量召回本身很快，慢的一般是
rerank 模型的上游；把 `match_count` 调小或在 WeKnora 侧关掉 rerank 都能缓解。

**片段太长把上下文撑爆**
调小 `match_count` 或 `max_chars_per_chunk`。WeKnora 的父块默认 4096 字符，3 条片段
不截断就是上万字符。
