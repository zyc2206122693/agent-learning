# Agent 学习 · 第 1 天笔记：工具调用 + 编排循环

> 记录日期：2026-08-16
> 目标：应聘 **Agent 工程师**。用理财看板项目做渐进式学习。
> 本课：把看板的「正则分类」升级为「LLM 结构化输出（工具调用）」，并搭好通用 Agent 骨架。

---

## 一、背景：你的简历 vs Agent 工程师要求

**你的强项**：美团 MaaS 负责人，大模型统一接入 / 路由 / 工具调用 / 流式 / 容灾 / 成本治理，`Claude Code` 深度实践。

**主要缺口（不是"造平台"，而是"亲自写 Agent"）**：
- Agent 编排（规划→调用→观察→决策的循环）
- Agent 评估（回放、回归、评测）
- 拿得出手的端到端 Agent 作品

**面试官唯一担心的一句**："你会不会只是管平台，自己没写过 Agent 的思考过程？"
**答题策略**：每道题都落到 `run_tool_loop` 里你亲手写的代码上——这就是"我真写过"的硬证据。

---

## 二、本课产出（项目新增文件）

| 文件 | 作用 |
|---|---|
| `agent_tools.py` | 通用工具注册表：`Tool`（name/description/JSON Schema/executor）+ `ToolRegistry`，可导出成 Anthropic `tools` 数组 |
| `llm_client.py` | 裸 HTTP 调 DeepSeek **Anthropic 兼容接口**（零依赖）；`run_tool_loop` 编排循环骨架；离线 `MockLLM` 兜底 |
| `news_classifier.py` | 用 `tool_choice` 强制"分类工具"做结构化输出，替换正则 `tag_news`；真模型→mock→正则三级兜底 |
| `demo_classify.py` | `python3 demo_classify.py`（真模型） / `--mock`（离线）正则 vs LLM 分类对比 |
| `config.json` | 存 key / base_url / model / use_llm / news_llm_classify（已 gitignore） |

**关键接口（DeepSeek）**：
- base_url: `https://api.deepseek.com/anthropic`，实际调用地址 `.../anthropic/v1/messages`
- 认证: 请求头 `x-api-key`（`anthropic-version` 被忽略但照发）
- 模型: `deepseek-chat`（工具可用）；`deepseek-reasoner` **不支持** tools
- 无状态：每轮必须全量重发 `messages`

---

## 三、工具调用：核心心智模型

> **工具 = 一份"说明书"（contract），不是代码。** 模型读说明书、点头名（tool_use）、报参数；**真正执行的是你**；你把结果（tool_result）喂回去，模型才继续。

**完整往返（4 步）**：
```
你 → 模型:  messages + tools=[说明书] + tool_choice
模型 → 你:  content=[ {type:"tool_use", name, id, input:{...}} ]   ← 模型"点名"，没执行任何东西
你 → 模型:  content=[ {type:"tool_result", tool_use_id, content, is_error} ]  ← 你执行，回填结果
模型 → 你:  content=[ {type:"text", text:...} ]   ← 信息够了，返回最终答案
```

**4 个关键概念**：
- **tools**：工具的"说明书"（name + description + input_schema），模型**读它来决定调不调**，从不碰你的真实函数。
- **tool_choice**：
  - `auto`：模型自己决定（问答型 Agent 默认）
  - `tool`：强制调指定工具（分类/结构化输出场景）——分类器就用这个
  - `any`：强制至少调一个；`none`：禁用工具
- **tool_use block**：模型"点名"——想调哪个、传什么参。**此时未执行**。
- **tool_result block**：你执行完，用同一个 `tool_use_id` 以 `user` 角色回填。**必须成对**，缺了上下文就断。

**几个要点**：
- `description` + `input_schema`（type/enum/required）就是"喂给模型的 prompt"，约束越死，模型越不容易填错。
- 工具出错不静默 fail，而是 `is_error:true` 回填，让模型"看到错误→自我修正"。
- 纯结构化抽取用 JSON mode 更省；工具调用的独特价值在"根据前结果动态决定下一步"。

---

## 四、run_tool_loop：Agent 的灵魂（`llm_client.py`）

```
输入: registry(工具), system, messages, tool_choice, max_steps=6
循环 for _ in range(max_steps):
  ① 全量历史 + 说明书 发给模型（complete）
  ② 模型没返回 tool_use → 正常出口：final_text = 拼所有 text block，return
  ③ 有 tool_use → for 循环执行每个工具，结果包成 tool_result 回填进 messages
     （try/except：出错也回填 is_error:true）
  超 max_steps → 兜底：从历史拼 text 返回，标记 truncated:true
返回: { final_text, transcript, steps, tool_calls, truncated? }
```

**设计点（面试要能讲）**：
1. **无状态**：每轮全量重发 `messages`，所以上下文会涨 → 上层做上下文管理（摘要/滑动窗口/prompt cache，接你 MaaS 的 Session Affinity 缓存经历）。
2. **终止条件**：`没有 tool_use 且返回 text` = 收敛。正常出口就是 ②。
3. **安全阀**：`max_steps` 防无限循环；超限兜底返回部分结果 + `truncated:true`，绝不挂死。
4. **容错**：工具异常以 `is_error:true` 回填，模型可自我修正。
5. **可测试**：`MockLLM` 输出与真模型同构但确定 → 离线跑断言；`transcript` 记录每轮 → 回放分析。这就是"Agent 也要可回归、可观测"。

**实测结果**：真模型跑 2 个并行工具（查基金 + 查新闻）→ 3 轮收敛（第1轮 2 个 tool_use → 第2轮回填 → 第3轮 text），`tool_calls=2`。

---

## 五、并行调用 & 慢工具的坑（重点！）

**先纠正**：`run_tool_loop` 现在的"并行"只在**协议层**是真的（模型一次返回多个 tool_use），但执行段是 `for` 循环 = **串行**（`llm_client.py:207`）。

**耗时对比**（10ms / 1s / 30s 三个工具）：
- 串行（当前）：≈ 10ms + 1s + 30s ≈ **31s**
- 并发：≈ **30s**（取最大值）

**但救不了 30s 的工具**——协议"全有或全无"：
> 模型返回 N 个 `tool_use`，你必须回齐 N 个 `tool_result` 才能发起下一轮。所以即使并发，也得**等最慢的那个跑完**。30s 是硬等待。

**本质**：并行把耗时从「和」→「最大值」，优化的是"快的工具别拖累你"，绕不开"最慢的那个"。

**正确解法（攻击慢工具，四招）**：
1. **给工具设独立超时**（最关键）——现在 `complete()` 有超时，但工具 executor 没有，挂死会卡死整个 loop。
2. **超时/失败就降级**——`is_error:true` 回填，让模型带已有结果继续，别死等。
3. **结果缓存**——慢工具结果可复用就缓存，第二次毫秒级。
4. **从源头变快**——分页、只取必要字段、拆小工具，别让一个工具扛 30s。

**面试答题版**：
> 并行把耗时从「和」变「最大值」，但协议要求一次性回齐所有结果，所以最慢的工具仍是硬瓶颈。真正的解法是给工具设超时、失败降级、结果缓存、以及把工具从源头做快——而不是指望并行。

---

## 六、面试题卡（题目 / 考查点 / 答案）

### Q1. tool calling 完整往返流程，模型"执行"了吗？
- **考查点**：是否真懂"模型只读说明书、执行权在调用方"。
- **答**：四步往返。① 发 `tools` 说明书；② 模型回 `tool_use`（点名+参数，**未执行**）；③ 我执行，包成 `tool_result` 用同一 `tool_use_id` 回填；④ 模型继续直到信息够才回纯文本。关键：**模型只决策，执行永远是我的代码**。

### Q2. `tool_choice` 有哪几种？什么场景用哪种？
- **考查点**：`none/auto/any/tool` 语义与适用场景。
- **答**：`none` 禁用；`auto` 模型自定（问答默认）；`any` 强制至少调一个；`tool` 强制调指定（分类走结构化输出用这个）。投资助手用 `auto` 让模型自主编排。

### Q3. 模型怎么知道参数怎么填？schema 写不好会怎样？
- **考查点**：input_schema 就是喂给模型的 prompt。
- **答**：靠 `description`（说明书）+ `input_schema` 的 type/enum/required 约束取值范围。分类工具 `category` 用 `enum`，模型只能四选一。schema 含糊→模型补非法参数，所以解析时做 `_coerce` 兜底。

### Q4. 工具执行出错怎么办？
- **考查点**：Agent 容错闭环。
- **答**：不静默 fail，把错误当结果回填（`is_error:true` + 异常信息），模型看到报错自己换参重试或换工具。无解错误再外层兜底降级。

### Q5. 为什么每轮全量重发历史？上下文无限涨怎么办？
- **考查点**：无状态协议 + 上下文/成本治理。
- **答**：接口无状态，必须全量带 `messages`。为防烧 token，上层做上下文管理：摘要/滑动窗口/prompt cache。接我 MaaS 的 Session Affinity 缓存经历（命中率 30%→80%）。

### Q6. 什么时候收敛？空转怎么办？
- **考查点**：终止条件 + 安全阀 + 超限兜底。
- **答**：收敛 = 无 `tool_use` 且返回 text。`max_steps`(6) 防失控，超限从历史拼 text 兜底返回 + `truncated:true`。宁可给部分结果，不让 Agent 无限烧钱。

### Q7. 并行 vs 串行？并行的坑？
- **考查点**：并行省轮次 vs 依赖问题。
- **答**：并行一次执行多个 tool_use 再一起回填，省往返。坑在**依赖**：工具 B 参数要 A 结果就不能并行，要分阶段/串行。独立查询可并行，有依赖必须编排成多步。

### Q8. "查持仓→查新闻→再分析"拆一个还是多个 Agent？
- **考查点**：单 Agent 多工具 vs 多 Agent 编排的权衡。
- **答**：线性同上下文 → 单 Agent 多工具（编排简单、调试容易）。角色差异明显（研究员/顾问/总结）才拆多 Agent，上游产出**结构化中间结果**传下游，设收敛出口。**先单 Agent，真正需要再拆**。

### Q9. 什么时候**不该**用工具调用？tool calling vs JSON mode？
- **考查点**：是否无脑堆工具，是否懂权衡。
- **答**：纯结构化抽取用 JSON mode 更省更简单。工具调用价值在"根据前结果动态决定下一步"。**会取舍、不炫技**。

### Q10. loop 输出不可控，怎么测？防回归？
- **考查点**：Agent 评测/可靠性。
- **答**：① `MockLLM` 确定性测试跑断言；② `transcript` 回放分析每一步 tool_use；③ 平时用 mock 隔离成本，验证真模型时才切真实 key。Agent 也要可回归、可观测。

---

## 七、安全提醒

- 🔐 API key：`sk-cfd...6fef` 已出现在对话里，**用一阵后建议在 DeepSeek 控制台轮换**。代码/日志只显示 `****6fef`，不打印明文。密钥存 `config.json`（已 gitignore）。
- ⚠️ `news_data.py` 实时看板默认走正则（`news_llm_classify=false`），避免每条新闻都烧 API；LLM 分类走显式/demo 路径。

---

## 八、下一步路线图（供白天规划）

- **第 2 步**：意图路由
- **第 3 步**：投资助理编排循环（把真数据 `funds.json` + `news_data` 接成真工具，让模型自主查→答）
- **第 4 步**：多 Agent + 评估
- **可选加固**：把 `run_tool_loop` 从串行升级为**真并发**（`ThreadPoolExecutor` + 每个工具独立超时 + 失败降级）

---

*个人 Agent 学习笔记，非投资建议。*
