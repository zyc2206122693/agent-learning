#!/usr/bin/env python3
"""Agent 第 3 课：评估 —— 给多 Agent 的"意图路由"做回归测试。

Agent 输出不可控，所以要有评测：给定一批问题 + 期望的意图/工具调用模式，
跑协调器，断言结果是否符合预期。这比只看"它答得像不像"可靠得多。

- 默认用 MockLLM（确定性、不烧钱），验证路由机制稳定；
- 加 --real 用真模型验证真实路由准确性；
- 目标是：改了一个工具/提示词，不会悄悄把别的链路带崩（回归）。

用法：
  python3 evaluate.py            # mock 确定性回归
  python3 evaluate.py --real     # 真模型验证路由
"""

from __future__ import annotations

import argparse
import os
import sys
from typing import Any, Dict, List

from llm_client import LLMConfig, make_client
from multi_agent import route_intents

# 评测集：(问题, 期望意图)
TEST_CASES: List[Dict[str, Any]] = [
    {"q": "整个组合现在怎么样？", "expected": ["holdings"]},
    {"q": "我持仓里的 040046 现在值多少？", "expected": ["holdings"]},
    {"q": "纳指100方向最近有什么新闻？", "expected": ["news"]},
    {"q": "我A股科技方向的持仓现在什么情况？", "expected": ["holdings"]},
    {"q": "我持仓怎么样？纳指有什么新闻？", "expected": ["holdings", "news"]},
]


def _match(pred: List[str], exp: List[str]) -> bool:
    return sorted(pred) == sorted(exp)


def main() -> None:
    ap = argparse.ArgumentParser(description="多 Agent 意图路由评测")
    ap.add_argument("--real", action="store_true", help="用真实模型评测（默认 mock）")
    args = ap.parse_args()
    if not args.real:
        os.environ["USE_LLM"] = "0"
    client = make_client()
    cfg = LLMConfig.load()
    print(f"评测模式: {type(client).__name__} · 模型={cfg.model}")
    print(f"评测集: {len(TEST_CASES)} 条\n" + "-" * 60)

    passed = 0
    for i, case in enumerate(TEST_CASES, 1):
        try:
            pred = route_intents(case["q"], client)
        except Exception as e:
            print(f"[{i}] {type(e).__name__}: {e}")
            continue
        ok = _match(pred, case["expected"])
        passed += ok
        mark = "PASS" if ok else "FAIL"
        print(f"[{mark}] 期望={case['expected']} 实际={pred}  | {case['q']}")

    print("-" * 60)
    print(f"结果: {passed}/{len(TEST_CASES)} 通过  "
          f"({passed / len(TEST_CASES) * 100:.0f}%)"
          + ("  ← 路由稳定" if passed == len(TEST_CASES) else "  ← 有回归，需排查"))
    sys.exit(0 if passed == len(TEST_CASES) else 1)


if __name__ == "__main__":
    main()
