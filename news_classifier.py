#!/usr/bin/env python3
"""Agent 第一课：用 LLM 结构化输出（工具调用）给新闻打标签。

把原来基于正则的 tag_news 升级为：定义一把「分类工具」（其 input_schema 就是
我们想要的输出结构），用 tool_choice 强制模型调用它，从而拿到结构化的
{category, sentiment, related}。这比 JSON mode 更贴近真实 Agent 场景，也把
「函数调用 + 结构化输出」做成可面试的 tool-calling 作品。

兜底链：真模型 → mock（离线演示链路）→ 正则（最坏情况保证有结果）。
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from agent_tools import Tool
from llm_client import (
    LLMConfig,
    LLMError,
    MockLLM,
    make_client,
)

# 与 news_data.py 保持一致的本字典/标签
CATEGORIES = ["宏观", "行业", "公司", "市场"]
SENTIMENTS = ["利好", "利空", "中性"]
RELATED_LABELS = ["纳指", "AI 主题", "机器人", "制造业", "资源", "债券"]

# 这把"工具"的输出结构 = 我们想要的分类结果
CLASSIFY_TOOL = Tool(
    name="classify_news",
    description=(
        "把一条财经新闻结构化分类。category 为宏观/行业/公司/市场 之一；"
        "sentiment 为利好/利空/中性 之一；related 列出新闻关联到的持仓方向（可为空数组）。"
    ),
    input_schema={
        "type": "object",
        "properties": {
            "category": {"type": "string", "enum": CATEGORIES, "description": "新闻所属分类"},
            "sentiment": {"type": "string", "enum": SENTIMENTS, "description": "情绪倾向"},
            "related": {
                "type": "array",
                "items": {"type": "string", "enum": RELATED_LABELS},
                "description": "关联的持仓方向标签，可空",
            },
        },
        "required": ["category", "sentiment", "related"],
    },
    executor=lambda **_: {"ok": True},  # 分类工具是"输出契约"，实际不执行，仅占位
)

# 强制调用该工具
CLASSIFY_TOOL_CHOICE = {"type": "tool", "name": CLASSIFY_TOOL.name}

_SYSTEM_PROMPT = (
    "你是财经新闻的结构化分类器。你只能调用 classify_news 工具输出结果，"
    "不要输出任何额外文字或解释。"
)


def _coerce(raw: Dict[str, Any]) -> Dict[str, Any]:
    """把模型返回的 input 规范成合法字段，防止枚举外取值/畸形结构。"""
    category = raw.get("category")
    category = category if category in CATEGORIES else "市场"
    sentiment = raw.get("sentiment")
    sentiment = sentiment if sentiment in SENTIMENTS else "中性"
    related = raw.get("related", [])
    if isinstance(related, str):
        related = [related]
    if not isinstance(related, list):
        related = []
    seen, cleaned = set(), []
    for r in related:
        if isinstance(r, str) and r in RELATED_LABELS and r not in seen:
            seen.add(r)
            cleaned.append(r)
    return {"category": category, "sentiment": sentiment, "related": cleaned}


def current_client():
    """返回当前生效的客户端（真实或 mock），供 demo 展示用。"""
    return make_client()


def classify_news_llm(
    title: str,
    summary: str = "",
    client: Optional[Any] = None,
) -> Dict[str, Any]:
    """用 LLM 工具调用做分类，返回 {category, sentiment, related}。

    失败（无密钥/网络/上游）抛 LLMError，由调用方回退。
    """
    client = client or make_client()
    text = (title + " " + summary).strip()
    resp = client.complete(
        system=_SYSTEM_PROMPT,
        messages=[{"role": "user", "content": f"请分类这条新闻：\n{text}"}],
        tools=[CLASSIFY_TOOL.as_anthropic()],
        tool_choice=CLASSIFY_TOOL_CHOICE,
        max_tokens=300,
        temperature=0.0,
    )
    for block in resp.get("content", []):
        if block.get("type") == "tool_use" and block.get("name") == CLASSIFY_TOOL.name:
            return _coerce(block.get("input", {}))
    raise LLMError("模型未返回 classify_news 工具调用")


def classify_news_rule(title: str, summary: str = "") -> Dict[str, Any]:
    """正则兜底分类，复用 news_data.tag_news（单一事实来源）。"""
    from news_data import tag_news  # 懒加载，避免循环导入

    category, sentiment, related = tag_news({"title": title, "summary": summary})
    return {"category": category, "sentiment": sentiment, "related": related}


def classify_news(
    title: str,
    summary: str = "",
    use_llm: Optional[bool] = None,
    client: Optional[Any] = None,
) -> Dict[str, Any]:
    """对外主入口：LLM 优先、正则兜底。use_llm 默认取 config.json 的开关。"""
    use_llm = use_llm if use_llm is not None else _config().get("use_llm", True)
    if not use_llm:
        return classify_news_rule(title, summary)

    client = client or make_client()
    try:
        # mock 也会成功返回结构（用于离线演示整条链路）
        return classify_news_llm(title, summary, client=client)
    except LLMError:
        return classify_news_rule(title, summary)


_config_cache: Optional[Dict[str, Any]] = None


def _config() -> Dict[str, Any]:
    global _config_cache
    if _config_cache is None:
        import json
        import os

        path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")
        try:
            with open(path, "r", encoding="utf-8") as f:
                _config_cache = json.load(f)
        except Exception:
            _config_cache = {}
    return _config_cache


if __name__ == "__main__":
    from llm_client import LLMConfig

    samples = [
        ("央行宣布降准0.5个百分点 释放长期资金约1万亿", ""),
        ("英伟达AI芯片需求旺盛 股价再创新高", ""),
        ("某半导体公司公告拟收购同行 增强产能布局", ""),
    ]
    cfg = LLMConfig.load()
    cli = make_client()
    print(f"[客户端] {type(cli).__name__} | model={cfg.model}")
    for title, summary in samples:
        llm = classify_news_llm(title, summary, client=cli)
        rule = classify_news_rule(title, summary)
        print("-" * 60)
        print("新闻:", title)
        print("  LLM :", llm)
        print("  正则:", rule)
