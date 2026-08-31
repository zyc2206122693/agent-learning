#!/usr/bin/env python3
"""Agent 第 2 课：投资助理 —— 真数据工具 + 编排循环。

第 1 课搭好了通用骨架（agent_tools + llm_client.run_tool_loop + MockLLM 兜底）。
这一课把"假工具"换成"真数据源"，让模型用 auto 自主决定要查哪些工具、再回答：

  工具（都来自真实数据）：
  - get_portfolio_summary : 组合总览（总市值/累计投入/收益率/方向分布）
  - get_fund_detail       : 单只基金详情（代码/市值/投入/收益/定投状态）
  - list_funds_by_theme   : 按方向模块（纳指100/A股科技/稳健固收/观察停投）查持仓
  - get_news              : 查真新闻（按关联方向过滤）

编排：LLMClient.run_tool_loop(auto) → 模型自己决定"查持仓还是查新闻" → 你执行真工具
→ 结果回填 → 模型汇总成回答。这就是"会自己用工具完成任务"的 Agent。

用法：
  python3 investment_assistant.py                    # 用内置示例问句
  python3 investment_assistant.py "040046 现在值多少" # 自定义问句
  python3 investment_assistant.py --mock "..."       # 离线演示链路
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any, Dict, List, Optional

from agent_tools import Tool, ToolRegistry
from fund_data import load_funds
from llm_client import LLMConfig, LLMError, MockLLM, make_client
from news_data import get_news
from portfolio_analysis import analyze_portfolio, simulate_rebalance, transaction_summary

THEMES = ["纳指100", "A股科技", "稳健固收", "观察停投", "全部"]
RELATED_LABELS = ["纳指", "AI 主题", "机器人", "制造业", "资源", "债券"]

_SYSTEM_PROMPT = (
    "你是用户的个人基金投资助理。回答前，先根据问题判断需要哪些信息，调用相关工具获取真实数据，"
    "再用数据回答，不要编造数据。问题涉及：整个组合用 get_portfolio_summary；某只基金用 get_fund_detail；"
    "某个方向模块用 list_funds_by_theme；风险/集中度用 analyze_portfolio；"
    "目标仓位调整用 simulate_rebalance；历史买卖记录用 get_transaction_summary；"
    "行情/最新动态用 get_news。数据以工具返回为准。调仓结果只是模拟，不要声称已执行交易。"
)


def _round2(x: Any) -> Any:
    if isinstance(x, (int, float)):
        return round(float(x), 2)
    return x


def _calc(f: Dict[str, Any]) -> Dict[str, Any]:
    """给单只基金补上收益/收益率字段。"""
    value, invested = f.get("amount", 0), f.get("total_invested", 0)
    ret = value - invested
    pct = ret / invested * 100 if invested else 0.0
    return {
        "code": f.get("code"),
        "name": f.get("name"),
        "theme": f.get("theme"),
        "platform": f.get("platform"),
        "value": _round2(value),
        "invested": _round2(invested),
        "return_amount": _round2(ret),
        "return_pct": _round2(pct),
        "investment_status": f.get("investment_status"),
    }


def build_all_tools() -> List[Tool]:
    """构造全部 4 个真数据工具，返回列表（供单 Agent 或多 Agent 按角色拆分复用）。"""

    # --- 工具 1：组合总览 ---
    def portfolio_summary() -> Dict[str, Any]:
        funds = (load_funds() or {}).get("funds", [])
        total_value = sum(f.get("amount", 0) for f in funds)
        total_invested = sum(f.get("total_invested", 0) for f in funds)
        ret = total_value - total_invested
        pct = ret / total_invested * 100 if total_invested else 0.0
        themes: Dict[str, Dict[str, float]] = {}
        for f in funds:
            t = f.get("theme", "其他")
            d = themes.setdefault(t, {"count": 0, "value": 0.0, "invested": 0.0})
            d["count"] += 1
            d["value"] += f.get("amount", 0)
            d["invested"] += f.get("total_invested", 0)
        theme_list = []
        for t, d in themes.items():
            theme_list.append({
                "theme": t,
                "count": d["count"],
                "value": _round2(d["value"]),
                "invested": _round2(d["invested"]),
                "return_pct": _round2((d["value"] - d["invested"]) / d["invested"] * 100 if d["invested"] else 0.0),
            })
        return {
            "total_value": _round2(total_value),
            "total_invested": _round2(total_invested),
            "return_amount": _round2(ret),
            "return_pct": _round2(pct),
            "fund_count": len(funds),
            "themes": theme_list,
        }

    # --- 工具 2：单只基金详情 ---
    def fund_detail(code: str) -> Dict[str, Any]:
        funds = (load_funds() or {}).get("funds", [])
        for f in funds:
            if f.get("code") == code:
                return _calc(f)
        return {"error": f"未找到代码为 {code} 的基金", "known_codes": [f["code"] for f in funds][:20]}

    # --- 工具 3：按方向模块查持仓 ---
    def funds_by_theme(theme: str) -> List[Dict[str, Any]]:
        if theme not in THEMES:
            return {"error": f"theme 必须是 {THEMES} 之一"}
        funds = (load_funds() or {}).get("funds", [])
        if theme != "全部":
            funds = [f for f in funds if f.get("theme") == theme]
        return [_calc(f) for f in funds]

    # --- 工具 4：查真新闻 ---
    def news(related: str, limit: int = 5) -> Dict[str, Any]:
        if related not in ["全部"] + RELATED_LABELS:
            return {"error": f"related 必须是 {['全部'] + RELATED_LABELS} 之一"}
        res = get_news()  # 缓存优先，便宜
        items = res.get("items", [])
        if related != "全部":
            items = [it for it in items if related in (it.get("related") or [])]
        rows = [{
            "time": it.get("time", "")[:16],
            "sentiment": it.get("sentiment"),
            "category": it.get("category"),
            "title": it.get("title", "")[:60],
        } for it in items[: max(1, min(limit, 10))]]
        return {"count": len(rows), "items": rows}

    return [
        Tool(name="get_portfolio_summary",
             description="查询整个基金组合的总览：总市值、累计投入、总收益/收益率、以及各方向模块(纳指100/A股科技/稳健固收/观察停投)的只数、市值和收益率。",
             input_schema={"type": "object", "properties": {}},
             executor=portfolio_summary),
        Tool(name="get_fund_detail",
             description="按 6 位基金代码查询某只基金详情：当前市值、累计投入、收益/收益率、定投状态。代码错误会返回已知代码列表。",
             input_schema={"type": "object", "properties": {"code": {"type": "string", "description": "6位基金代码，如 040046"}}, "required": ["code"]},
             executor=fund_detail),
        Tool(name="list_funds_by_theme",
             description="按方向模块查询该模块下所有持仓基金的明细（代码/市值/投入/收益/收益率）。theme 可选 纳指100/A股科技/稳健固收/观察停投/全部。",
             input_schema={"type": "object", "properties": {"theme": {"type": "string", "enum": THEMES, "description": "方向模块"}}, "required": ["theme"]},
             executor=funds_by_theme),
        Tool(name="get_news",
             description="查询最新财经快讯，可按关联方向过滤(纳指/AI 主题/机器人/制造业/资源/债券 或 全部)。返回时间、情绪(利好/利空/中性)、分类和标题。",
             input_schema={"type": "object",
                           "properties": {"related": {"type": "string", "enum": ["全部"] + RELATED_LABELS, "description": "关联方向"},
                                          "limit": {"type": "integer", "description": "最多返回条数，默认5"}},
                           "required": ["related"]},
             executor=news),
        Tool(name="analyze_portfolio",
             description="分析当前组合的主题占比、单基金集中度和中高风险基金占比，并按 portfolio_settings.json 的阈值给出提醒。",
             input_schema={"type": "object", "properties": {}},
             executor=analyze_portfolio),
        Tool(name="simulate_rebalance",
             description="按照 portfolio_settings.json 中的主题目标比例，模拟各方向需要买入、卖出或保持的金额；只计算，不执行交易。",
             input_schema={"type": "object", "properties": {}},
             executor=simulate_rebalance),
        Tool(name="get_transaction_summary",
             description="汇总 transactions.json 中已登记的买入、卖出、分红、费用和净投入。",
             input_schema={"type": "object", "properties": {}},
             executor=transaction_summary),
    ]


def build_registry() -> ToolRegistry:
    """单 Agent 版：把全部工具注册进一个 registry。"""
    reg = ToolRegistry()
    for t in build_all_tools():
        reg.register(t)
    return reg


def ask(question: str, client: Optional[Any] = None, max_tokens: int = 800,
        history: Optional[List[Dict[str, str]]] = None) -> Dict[str, Any]:
    """让投资助理回答一个自然语言问题（模型自主决定查哪些工具）。"""
    registry = build_registry()
    client = client or make_client()
    messages: List[Dict[str, Any]] = []
    for message in (history or [])[-8:]:
        role = message.get("role")
        content = message.get("content")
        if role in ("user", "assistant") and isinstance(content, str) and content.strip():
            messages.append({"role": role, "content": content[:3000]})
    messages.append({"role": "user", "content": question})
    return client.run_tool_loop(
        registry,
        system=_SYSTEM_PROMPT,
        messages=messages,
        tool_choice={"type": "auto"},
        max_tokens=max_tokens,
    )


def main() -> None:
    ap = argparse.ArgumentParser(description="投资助理：真实数据 + 工具调用编排")
    ap.add_argument("question", nargs="?", default="我持仓里的 040046 现在值多少？纳指100方向最近有什么新闻？",
                    help="要问的问题")
    ap.add_argument("--mock", action="store_true", help="离线 MockLLM 演示")
    args = ap.parse_args()

    if args.mock:
        import os
        os.environ["USE_LLM"] = "0"
    client = make_client()
    cfg = LLMConfig.load()
    print("=" * 70)
    print(f"投资助理 · 客户端={type(client).__name__} · 模型={cfg.model}")
    print(f"问: {args.question}")
    print("=" * 70)

    result = ask(args.question, client=client)
    print(f"\n[编排] 共 {result['steps']} 轮，调用工具 {len(result['tool_calls'])} 次")
    for i, tc in enumerate(result.get("tool_calls", []), 1):
        print(f"  {i}. {tc['name']}({json.dumps(tc['args'], ensure_ascii=False)})")
    if result.get("truncated"):
        print("  ⚠ 达到步数上限，结果为部分收敛")
    print("\n[回答]")
    print(result.get("final_text", "") or "(空)")


if __name__ == "__main__":
    main()
