#!/usr/bin/env python3
"""Agent 第一课演示：正则 vs LLM 结构化分类对比。

抓取看板里的最新新闻，对每一条并排打印「正则规则」和「LLM 工具调用」的分类结果，
直观看到这套 tool-calling + 结构化输出 的链路，以及 LLM 相比规则更"懂"新闻语义。

用法：
  python3 demo_classify.py            # 默认走真实模型（需 config.json 配好 key）
  python3 demo_classify.py --mock     # 强制离线，用 MockLLM 走通整条链路
  python3 demo_classify.py --n 8      # 展示前 8 条
"""

from __future__ import annotations

import argparse
import os

import news_data
from llm_client import LLMConfig, MockLLM, make_client, mask_key
from news_classifier import classify_news_llm, classify_news_rule


def main() -> None:
    ap = argparse.ArgumentParser(description="正则 vs LLM 新闻分类对比")
    ap.add_argument("--n", type=int, default=5, help="展示条数")
    ap.add_argument("--mock", action="store_true", help="强制使用离线 MockLLM")
    ap.add_argument("--refresh", action="store_true", help="强制刷新上游新闻")
    args = ap.parse_args()

    if args.mock:
        os.environ["USE_LLM"] = "0"
    client = make_client()
    cfg = LLMConfig.load()

    print("=" * 78)
    print("Agent 第一课 · 正则规则 vs LLM 工具调用 分类对比")
    print(f"  客户端 : {type(client).__name__}  模型: {cfg.model}")
    if isinstance(client, MockLLM):
        print("  ⚠ 离线 mock 模式：仅演示'工具调用→结构化输出'的机制，分类准确性请用真实模型")
    else:
        print(f"  API key: {mask_key(cfg.api_key)}")
    print("=" * 78)

    res = news_data.get_news(refresh=args.refresh)
    items = res["items"][: args.n]
    if not items:
        print("没有拉到新闻（可能网络问题），请稍后重试。")
        return

    same = 0
    for i, it in enumerate(items, 1):
        title, summary = it["title"], it["summary"]
        rule = classify_news_rule(title, summary)
        try:
            llm = classify_news_llm(title, summary, client=client)
            llm_err = None
        except Exception as e:
            llm, llm_err = None, f"{type(e).__name__}: {e}"
        if llm is not None and llm == rule:
            same += 1

        print(f"\n[{i}/{len(items)}] {title}")
        if summary:
            print(f"      摘要: {summary[:60]}")
        print(f"  正则 : 类别={rule['category']:<2} 情绪={rule['sentiment']:<2} 关联={rule['related']}")
        if llm is not None:
            print(f"  LLM  : 类别={llm['category']:<2} 情绪={llm['sentiment']:<2} 关联={llm['related']}")
            mark = "✓ 一致" if llm == rule else "↯ 差异(看LLM是否更合理)"
            print(f"          {mark}")
        else:
            print(f"  LLM  : 失败，已由正则兜底 → {llm_err}")

    print("\n" + "-" * 78)
    print(f"小结：{len(items)} 条中 {same} 条 正则与 LLM 结论一致，其余看 LLM 是否更贴合语义。")


if __name__ == "__main__":
    main()
