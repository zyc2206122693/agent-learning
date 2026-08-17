#!/usr/bin/env python3
"""Agent 第 3 课：多 Agent 编排 —— 协调器(意图路由) + 专家 Agent + 收敛。

把"一个 Agent 调所有工具"升级为"多个专职 Agent"：
  1. 协调器（意图路由）: 复用第 1 课的结构化输出，把用户问题分类成意图
     {"holdings"(查持仓) / "news"(查新闻)}，可能命中多个。
  2. 专家 Agent: 每个意图对应一个专职 Agent，它只有自己的工具子集 + 系统提示，
     各自跑一轮 run_tool_loop（auto 自主调自己的工具）。
  3. 收敛 Agent: 把多个专家的输出汇总成一份回答（生产里就是"总结/收敛"出口）。

设计要点（面试可讲）：
  - 每个专家只看到自己的工具 → 上下文更小、更专注、不易误调。
  - 专家之间通过"结构化中间结果"(final_text) 传给下游，而非自由文本。
  - 先单 Agent，角色差异真正需要时才拆多 Agent。

用法：
  python3 multi_agent.py                                   # 内置复合问句
  python3 multi_agent.py "040046 现在值多少？纳指有什么新闻？"
  python3 multi_agent.py --mock "..."
"""

from __future__ import annotations

import argparse
import json
import os
from typing import Any, Dict, List, Optional

from agent_tools import Tool, ToolRegistry
from investment_assistant import build_all_tools
from llm_client import LLMConfig, make_client

INTENTS = ["holdings", "news"]

# --- 协调器：意图路由（结构化输出） ---
ROUTE_TOOL = Tool(
    name="route_intent",
    description="判断用户问题需要哪些信息：holdings=需要查基金持仓/组合/市值收益；news=需要查最新行情新闻。可同时选多个。",
    input_schema={
        "type": "object",
        "properties": {"intents": {"type": "array", "items": {"type": "string", "enum": INTENTS}, "description": "命中的意图列表"}},
        "required": ["intents"],
    },
    executor=lambda **_: {"ok": True},
)
ROUTE_CHOICE = {"type": "tool", "name": "route_intent"}
ROUTE_SYSTEM = "你是路由协调器。只调用 route_intent 工具输出用户的意图，不要输出其他内容。"

# --- 专家定义：各自系统提示 + 工具子集 ---
EXPERTS: Dict[str, Dict[str, Any]] = {
    "holdings": {
        "system": "你是用户的基金持仓顾问。回答前先调用工具读取真实持仓数据，用数据说话，不要编造。",
        "tools": ["get_portfolio_summary", "get_fund_detail", "list_funds_by_theme"],
    },
    "news": {
        "system": "你是金融新闻研究员。回答前先调用 get_news 查询最新快讯，基于真实新闻给出方向性判断。",
        "tools": ["get_news"],
    },
}


def _coerce_intents(raw: Dict[str, Any]) -> List[str]:
    intents = raw.get("intents", [])
    if isinstance(intents, str):
        intents = [intents]
    seen, out = set(), []
    for i in intents:
        if isinstance(i, str) and i in INTENTS and i not in seen:
            seen.add(i)
            out.append(i)
    return out or ["holdings"]  # 默认兜底


def route_intents(question: str, client: Any) -> List[str]:
    resp = client.complete(
        system=ROUTE_SYSTEM,
        messages=[{"role": "user", "content": question}],
        tools=[ROUTE_TOOL.as_anthropic()],
        tool_choice=ROUTE_CHOICE,
        max_tokens=200,
        temperature=0.0,
    )
    for block in resp.get("content", []):
        if block.get("type") == "tool_use" and block.get("name") == "route_intent":
            return _coerce_intents(block.get("input", {}))
    return ["holdings"]


def _expert_registry(intent: str) -> ToolRegistry:
    allowed = set(EXPERTS[intent]["tools"])
    reg = ToolRegistry()
    for t in build_all_tools():
        if t.name in allowed:
            reg.register(t)
    return reg


def run_expert(intent: str, question: str, client: Any, max_tokens: int = 600) -> Dict[str, Any]:
    res = client.run_tool_loop(
        _expert_registry(intent),
        system=EXPERTS[intent]["system"],
        messages=[{"role": "user", "content": question}],
        tool_choice={"type": "auto"},
        max_tokens=max_tokens,
    )
    return {"intent": intent, "final_text": res.get("final_text", ""), "tool_calls": res.get("tool_calls", [])}


def synthesize(expert_results: List[Dict[str, Any]], client: Any, max_tokens: int = 500) -> str:
    """收敛 Agent：把多个专家的输出汇总成一份回答。"""
    if len(expert_results) == 1:
        return expert_results[0]["final_text"]
    blocks = "\n\n".join(
        f"[{r['intent']} 专家]\n{r['final_text']}" for r in expert_results
    )
    resp = client.complete(
        system="你是投资助理的总结者，把下面各领域专家给的材料合并成一份连贯、有条理、对用户有用的回答。",
        messages=[{"role": "user", "content": f"请汇总以下材料：\n\n{blocks}"}],
        max_tokens=max_tokens,
    )
    return "".join(b.get("text", "") for b in resp.get("content", []) if b.get("type") == "text")


def ask(question: str, client: Optional[Any] = None) -> Dict[str, Any]:
    client = client or make_client()
    intents = route_intents(question, client)
    expert_results = [run_expert(i, question, client) for i in intents]
    final = synthesize(expert_results, client)
    return {
        "question": question,
        "intents": intents,
        "expert_results": expert_results,
        "final_text": final,
    }


def main() -> None:
    ap = argparse.ArgumentParser(description="多 Agent 投资助理：协调器 + 专家 + 收敛")
    ap.add_argument("question", nargs="?", default="我持仓里的 040046 现在值多少？纳指100方向最近有什么新闻？我该关注什么？")
    ap.add_argument("--mock", action="store_true", help="离线 MockLLM 演示")
    args = ap.parse_args()
    if args.mock:
        os.environ["USE_LLM"] = "0"
    client = make_client()
    cfg = LLMConfig.load()
    print("=" * 70)
    print(f"多 Agent · 客户端={type(client).__name__} · 模型={cfg.model}")
    print(f"问: {args.question}")
    print("=" * 70)

    r = ask(args.question, client=client)
    print(f"\n[路由] 意图 → {r['intents']}")
    for er in r["expert_results"]:
        tools = [tc["name"] for tc in er["tool_calls"]]
        print(f"  [{er['intent']} 专家] 调用工具: {tools if tools else '(无)'}")
    print("\n[收敛回答]")
    print(r["final_text"] or "(空)")


if __name__ == "__main__":
    main()
