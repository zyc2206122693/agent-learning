# Agent 学习 · 第 3 天笔记：多 Agent 编排 + 评估

> 记录日期：2026-08-17
> 承接：第 1 课（结构化分类）、第 2 课（单 Agent 自主编排）。本课升级为**多 Agent**，并补上**评估**——高档位 Agent 面试的关键分。

---

## 一、本课目标

把"一个 Agent 调所有工具"升级为"多个专职 Agent"，并给 Agent 加"评测/回归"。

**三课递进（面试可讲完整链路）**：
- 第 1 课：`tool_choice: tool` 强制 → **结构化输出**（分类契约）
- 第 2 课：`tool_choice: auto` 自主 → **单 Agent 编排**
- 第 3 课：多 Agent + 评估 → **分工协作 + 可靠可测**

---

## 二、新增文件

### `multi_agent.py` — 三级结构

```
用户问题
  → ① 协调器（意图路由 route_intent，复用第1课结构化输出）
       → 意图: holdings(查持仓) / news(查新闻)，可多选
  → ② 专家 Agent（每个只带自己的工具子集 + 系统提示，各跑一轮 run_tool_loop）
       holdings 专家: get_portfolio_summary / get_fund_detail / list_funds_by_theme
       news 专家   : get_news
  → ③ 收敛 Agent（把多专家 final_text 汇总成一份回答）
```

**设计要点（面试可讲）**：
- 每个专家只看到自己的工具 → **上下文更小、更专注、不易误调**。
- 专家之间通过**结构化中间结果**（final_text）传给下游，而非自由文本。
- **先单 Agent，角色差异真正需要时才拆多 Agent**（承接第 1 课 Q8）。

**真模型实测**（复合问句）：
- 协调器路由到 `['holdings','news']`
- holdings 专家调 3 个真工具、news 专家调 2 个真工具
- 收敛 Agent 整合成连贯回答（040046 市值/收益 + 纳指持仓分化 + 新闻）

### `evaluate.py` — 评估/回归

- 评测集：5 条 (问题, 期望意图)，断言路由结果。
- `python3 evaluate.py`（mock 确定性，不烧钱）/ `python3 evaluate.py --real`（真模型）。
- 目标：改工具/提示词不会悄悄带崩别的链路（回归）。

---

## 三、本课最重要的教训：评估抓出了歧义

**第一次真模型评估 4/5**。FAIL 的那条：
> "A股科技方向现在什么情况？" — 期望 `holdings`，模型路由到 `news`。

**不是模型错了，是测试用例有歧义**：这句话既能指"我 A股科技 持仓"（holdings），也能指"A股科技板块行情"（news）。模型选了后者。

**没有评估，你根本发现不了这种歧义。** 正确做法不是改模型，而是把测试用例改**无歧义**（"我A股科技方向的持仓现在什么情况？"），重跑后 **5/5 全过**。

**这就是"评估驱动迭代"**：评测不只是"测对不对"，而是帮你发现边界情况、再改进用例/提示词。

---

## 四、面试答题要点

**Q8. "查持仓→查新闻→再分析"拆一个还是多个 Agent？**
- 线性同上下文 → 单 Agent 多工具；角色差异明显（研究员/顾问/总结）才拆多 Agent。
- 拆了之后：上游 Agent 产**结构化中间结果**传下游，设**收敛/总结出口**。
- **先单 Agent，真正需要再拆**。本课 `multi_agent.py` 就是"拆了之后长什么样"的活例子。

**Q10. Agent 输出不可控，怎么测？**
- 确定性测试（`MockLLM` 输出同构、可断言）；
- 回放（`transcript` 记录每轮 tool_use）；
- 评测集断言（`evaluate.py`：输入 → 期望意图/工具 → 比对）；
- 平时 mock 隔离成本，验证真模型才切真实 key。
- 本课案例：评估抓出歧义用例 → 改无歧义 → 5/5。

---

## 五、路线图

前 3 课已完成（结构化分类 / 单 Agent 编排 / 多 Agent + 评估）。
- **可选加固**：`run_tool_loop` 从串行升级为**真并发**（`ThreadPoolExecutor` + 每工具独立超时 + 失败降级）——正好补第 1 课 Q4 的"慢工具硬瓶颈"坑。

---

## 六、遗留重点（复习提醒）

- **Q4 并发耗时**：并发=取最大值（30s），串行=累加（31.1s）；慢工具是硬瓶颈 → 攻击它（超时/降级/缓存/做快）。
- **Q2**：`tool_result` 不需要符合 `input_schema`（那是入参格式）；要用 `tool_use_id` 对上号回填。
- **多 Agent 拆分的判断**：不是"越多越好"，是"角色差异 + 需要不同上下文/专业视角"才拆。

---

## 七、`multi_agent.py` 代码详解

**顶层数据流**：用户问句 → ① 协调器路由 → ② 各专家（每意图一个 run_tool_loop）→ ③ 收敛合成。

### ① 协调器 `route_intents()`（意图路由）
```python
ROUTE_TOOL = Tool("route_intent", input_schema={"intents": {"enum": ["holdings","news"]}}, ...)
ROUTE_CHOICE = {"type": "tool", "name": "route_intent"}   # 强制走结构化输出

def route_intents(question, client):
    resp = client.complete(system=ROUTE_SYSTEM, messages=[user question],
                           tools=[ROUTE_TOOL], tool_choice=ROUTE_CHOICE, ...)
    # 解析 tool_use 的 input，_coerce_intents 过滤非法/去重/空则默认 ["holdings"]
```
- **复用第 1 课"强制结构化输出"的套路**：路由本质就是一个分类问题。
- 用 `tool_choice: tool` 强制模型走 `route_intent` 工具 → 返回的一定是合法枚举，不会随口编。
- `_coerce_intents()` 兜底：过滤枚举外值、去重、空 → `["holdings"]`。

### ② 专家 `run_expert()` / `_expert_registry()`
```python
EXPERTS = {
    "holdings": {"system": "你是基金持仓顾问...", "tools": ["get_portfolio_summary","get_fund_detail","list_funds_by_theme"]},
    "news":     {"system": "你是金融新闻研究员...", "tools": ["get_news"]},
}
def _expert_registry(intent):
    return 从 build_all_tools() 里只注册 EXPERTS[intent]["tools"] 的工具
def run_expert(intent, question, client):
    return client.run_tool_loop(_expert_registry(intent), system=EXPERTS[intent]["system"],
                                tool_choice={"type": "auto"}, ...)
```
- **每个专家"只见自己的工具"** → 上下文更小、更专注、不易误调。
- **每个专家有独立 system prompt 定义角色**；同一个 `run_tool_loop` 骨架，换工具子集 + 换系统提示 = 不同 Agent。

### ③ 收敛 `synthesize()`
```python
if len(expert_results) == 1: return 直接用
else: 把各专家 final_text 拼接 → 再调一次 LLM 让"总结者"合并成连贯回答
```
- 一个专家直接返回（省调用）；多个专家由收敛 Agent 合并成**一份有逻辑的回答**。
- 这就是面试"多 Agent 结果怎么传、谁负责汇总"的答案。

### ④ `ask()` 编排入口
`route_intents → [run_expert for each intent] → synthesize`。注意专家是**串行**跑的（列表推导），互相独立的专家其实可并发优化（呼应 Q4 并发话题）。

### 单 Agent vs 多 Agent（为什么值得拆）
| | 单 Agent(第2课) | 多 Agent(第3课) |
|---|---|---|
| 工具 | 全塞给一个 | 每个只见子集 |
| 上下文 | 大而全 | 更小更专注 |
| 分工 | 一个角色 | 顾问/研究员/总结者各司其职 |
| 误调风险 | 高 | 低 |
| 成本 | 少 | 多（每专家调 LLM） |
> 拆的代价是成本，收益是专注与可靠 → **先单 Agent，角色差异真正需要时才拆**。

---

## 八、`evaluate.py` 代码详解

**测的就是协调器 `route_intents` 的路由决策**。
```python
TEST_CASES = [ {"q": "...", "expected": ["holdings"]}, ... ]   # (问题, 期望意图)

def main():
    if not --real: os.environ["USE_LLM"]="0"   # 默认 mock，不烧钱
    for case in TEST_CASES:
        pred = route_intents(case["q"], client)
        ok = sorted(pred) == sorted(case["expected"])   # 顺序无关、精确匹配
        PASS/FAIL
    # 全过才 exit 0
```

**为什么测路由、不测专家回答？**
- 自由文本输出**无法稳定断言**（两种说法都对）；结构化决策（意图）**确定、便宜、可断言**。
- Agent 评测核心思路：**能结构化、能确定的先测**。

**mock vs --real**
- `evaluate.py`（默认）：mock 关键词启发式路由，确定 → 验证**机制/链路**稳定，可放 CI。
- `evaluate.py --real`：真模型 → 验证**真实路由准确率**。

**评估驱动迭代（本课核心教训）**
- 第一次 `--real` 4/5。FAIL 的是歧义问句"A股科技方向现在什么情况？" → 期望 holdings，模型路由到 news。
- **不是模型 bug，是测试用例有歧义**（可指持仓也可指行情）。没有评估就发现不了。
- 修正：改测试用例为无歧义（"我A股科技方向的持仓..."）→ **5/5 全过**。而不是去改模型提示词。

**诚实的边界（面试主动说）**
`evaluate.py` 只测"路由对不对"，**没测**：① 专家工具调用模式；② 最终回答质量。
真实系统要用：**工具调用断言**（记录每轮 tool_use 比对）+ **LLM 裁判/人工评测**（自由文本打分）。
> 能主动说出"评估覆盖到哪层、还缺哪层"，比装作全测了更加分。

---

## 九、面试总结（Q10 完整答案）

> Agent 输出不可控，所以我分层测：**结构化决策**（意图路由）用评测集断言 + mock 确定性回归；**工具调用模式**记录每轮 tool_use 比对；**自由文本质量**用 LLM 裁判/人工评测。我的 `evaluate.py` 是第一层的落地——它真抓出过一个歧义用例，改无歧义后从 4/5 到 5/5。**Agent 也是要可回归、可评估的。**

---

*个人 Agent 学习笔记，非投资建议。*
