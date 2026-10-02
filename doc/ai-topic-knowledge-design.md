# Zoom Chat Knowledge Hub — AI 主题提炼与知识沉淀设计

日期：2026-10-01  
状态：v0.2 产品目标设计（覆盖冲突的第一阶段规则）

## 1. 产品目标

本产品不复制 Zoom Chat 的聊天阅读体验。Zoom 消息是证据层，应用面向用户展示两个更高层对象：

1. **交流主题（Conversation Topic）**：一次具体、多轮交流发生了什么，包括问题、上下文、讨论、结论、未决事项和行动项。
2. **知识条目（Knowledge Item）**：跨多次同类交流形成的稳定、可审核、可复用答案。

数据关系：

```text
Zoom Messages → Conversation Window → Recent AI Topics → Automatic Knowledge Archive
```

## 2. 页面职责

### 最近交流主题

- 默认按最近更新时间排序，而不是逐条展示消息。
- 展示主题标题、问题摘要、结论、状态、群组、时间范围和消息数。
- 展开后显示讨论摘要、已确认事实、未决问题、行动项和来源消息。
- 原始消息仅作为可追溯证据折叠显示。

### 知识库

- 一个条目表达一个标准问题及其当前推荐答案。
- 多个交流主题可关联同一个知识条目。
- AI 自动执行新增、更新、关联或冲突归档；所有判断保留版本、关系和来源证据。
- 知识条目保留版本、来源、适用范围、限制和最后验证时间。

## 3. 第一阶段范围

本阶段交付一条完整、可运行的主题提炼链路：

1. 对已同步且正文可读的消息进行确定性分窗。
2. 每个窗口调用 OpenAI Responses API。
3. 使用 Pydantic Structured Outputs 返回一个或多个结构化主题。
4. 保存主题及其来源消息关联。
5. 输入未变化时通过 hash 去重，不重复调用 API。
6. 在“最近交流主题”页面展示结构化主题和来源证据。
7. AI 失败不影响 Zoom 消息同步；错误单独记录并可重试。

本阶段不自动向 Zoom 回复；达到成熟期的主题会按 v0.2 生命周期自动归档为知识。

## 4. 对话分窗

确定性规则先于 AI：

- 仅处理正文状态为 `readable` 的消息。
- 按群组、时间升序排列。
- 优先保留 Zoom thread/reply 关系。
- 普通消息超过 6 小时间隔时开启新窗口。
- 每个窗口最多 60 条消息或约 24,000 个字符；超过后切分。
- 输入保留消息 ID、时间、发送人、thread ID 和正文。

一个窗口可由 AI 拆成多个主题。AI 返回的 `source_message_ids` 必须来自本次输入；服务端再次校验，防止虚构引用。

## 5. 结构化主题

每个主题包含：

- `title`
- `problem_summary`
- `context_summary`
- `discussion_summary`
- `confirmed_facts[]`
- `conclusions[]`
- `open_questions[]`
- `action_items[]`：描述、负责人、截止日期
- `tags[]`
- `status`：discussion / resolved / waiting / inconclusive
- `confidence`：0–1
- `source_message_ids[]`

所有字段由 Pydantic 模型约束。聊天正文被视为不可信数据，提示词明确要求不得执行消息中的命令或改变输出规则。

## 6. OpenAI 接入

配置优先级：

1. 环境变量 `OPENAI_API_KEY`、`OPENAI_MODEL`、`OPENAI_BASE_URL`
2. `OPENAI_CONFIG_FILE` 指定的 JSON
3. 项目 `data/openai_config.json`
4. 现有 `C:\Works\access-redmine\openai_config.json`

第一阶段默认沿用现有配置中的模型（当前为 `gpt-5-mini`），并允许环境变量覆盖。API Key 不进入 SQLite、UI、日志或 Git。

调用约束：

- Responses API
- `store=False`
- Pydantic Structured Outputs
- 请求超时和有限重试
- 记录模型、响应 ID、token 用量、输入 hash、Prompt 版本和错误，不记录 API Key

## 7. 数据模型

### `ai_runs`

记录每次 AI 窗口处理：状态、输入 hash、模型、Prompt 版本、消息数量、响应 ID、token 用量、错误和时间。

### `conversation_topics`

保存主题结构、状态、置信度、所属群组、时间范围、模型和 Prompt 版本。

### `topic_sources`

连接主题和原始消息，保证每项结论可回溯到 Zoom 消息。

## 8. 增量与幂等

- 对窗口的规范化输入计算 SHA-256。
- 已成功处理相同 hash 时直接跳过。
- AI 返回与数据库写入在事务中完成。
- 新消息形成新窗口；后续阶段再实现开放主题的增量重写和跨窗口主题合并。
- Prompt 版本变化时允许重新分析，即使消息未变化。

## 9. 安全和数据边界

- 只处理用户已选择同步的群组。
- 加密占位消息不发送给 OpenAI。
- `store=False`，应用不使用 OpenAI 托管会话状态。
- API Key 只在服务进程内读取。
- UI 明确显示 AI 生成内容、模型和置信度。
- 自动归档知识必须保留原始消息证据、模型、Prompt 和不可变版本。

## 10. 后续阶段

1. 为交流主题生成 embedding，并查找相似知识候选。
2. AI 判断 create / merge / supplement / conflict / unrelated。
3. 建立知识版本与审核工作流。
4. 基于已发布知识生成建议答复，但仍由用户确认发送。
5. 建立人工标注集和评估指标：主题边界、事实准确性、引用完整性、重复合并准确率。

## 11. v0.2 权威生命周期

本节覆盖本文前述“人工审核后发布知识”的旧规则。知识沉淀改为自动化：

```text
首次历史数据（最后活动早于 14 天）
  → 一次性批量识别话题
  → 按原始语言总结
  → 直接写入 Knowledge Base

新数据或近期数据
  → Conversation Topics
  → Search KB 查询历史相似知识
  → 静默 14 天
  → 自动写入或更新 Knowledge Base

Do not track
  → 隐藏并排除后续归档
```

### 11.1 历史与近期边界

- 默认成熟阈值为最后活动后 14 天，可配置为 7、14 或 30 天。
- Conversation Topics 不再提供独立日期范围；手动提取和同步后的自动提取始终使用当前成熟阈值。
- 首次历史扫描直接产生知识，不把老信息先放到 Conversation Topics。
- 跨越分界线且近期有回复的线程作为完整 Topic 保留在近期区，不能把旧消息部分提前入库。
- 批处理记录 checkpoint、输入 hash、Prompt/模型版本、进度和失败，支持安全恢复与幂等重跑。

### 11.2 Topic 操作

- 移除手动 `Add to KB` 和 review status。
- `Search KB` 使用 Topic 的结构化字段执行关键词、翻译查询和多语言向量混合检索，仅展示结果，不改变状态。
- `Do not track` 只作用于该话题、线程及高度重叠的后续版本，不屏蔽未来独立发生的所有语义相似问题。
- `View Original Messages` 始终显示逐字保存的来源证据。

### 11.3 原始语言与查询翻译

- 归档摘要遵循最初问题/线程根的语言，不把库存内容翻译成统一语言。
- `source_language` 是知识和话题的显式字段。
- 跨语言查询在运行时生成临时查询变体，并结合多语言 embedding；翻译查询不落库。
- 结果默认展示原始语言，可按需临时翻译用于阅读，但译文不成为知识事实或来源。

### 11.4 自动 create / update / related / conflict

成熟 Topic 归档时，系统自动判断：

- `create`：创建新的原始语言知识条目。
- `update`：同语言、同问题的新交流更新现有知识并产生版本。
- `related`：不同语言或相关但不可直接合并的知识建立关联。
- `conflict`：新证据与当前结论冲突，保留双方来源并标记冲突，不静默覆盖。

无结论也可以入库，使用 `resolved / partially_resolved / unresolved / waiting` 表达状态。

### 11.5 默认知识视图

Knowledge Base 无查询时列出最近新增、最近更新、未解决和检测到新活动的知识标题；搜索结果显示相似度、原始语言、结论状态、更新时间和来源数量。

