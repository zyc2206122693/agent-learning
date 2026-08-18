#!/usr/bin/env python3
"""Agent 第 1 课 Q4 的落地演示：并发 + 每工具独立超时。

直观验证两条结论：
  1. 并发执行 → 总耗时 ≈ 最慢工具（max），而不是串行的累加（sum）；
  2. 给每个工具独立超时 → 一个"挂死"的工具会被按失败回填，不再拖垮整轮。

不依赖真实模型，直接驱动 _execute_tool_blocks（run_tool_loop 里执行工具的那段）。

用法：
  python3 demo_concurrency.py
"""

from __future__ import annotations

import time

from agent_tools import Tool, ToolRegistry
from llm_client import _execute_tool_blocks


def make_tool(name: str, seconds: float):
    def fn():
        time.sleep(seconds)
        return {"name": name, "took": seconds}
    return Tool(name=name, description=name, input_schema={"type": "object", "properties": {}}, executor=fn)


def tool_use(name: str, tid: str) -> dict:
    return {"type": "tool_use", "id": tid, "name": name, "input": {}}


def fmt(seconds: float) -> str:
    return f"{seconds:.3f}s"


def main() -> None:
    # 快/中/慢 三个工具
    reg = ToolRegistry()
    fast = make_tool("fast", 0.05)
    med = make_tool("med", 0.30)
    slow = make_tool("slow", 1.50)
    hung = Tool(name="hung", description="挂死工具",
                input_schema={"type": "object", "properties": {}},
                executor=lambda: time.sleep(60))
    for t in (fast, med, slow, hung):
        reg.register(t)

    print("=" * 66)
    print("1) 并发执行：fast(0.05s) + med(0.30s) + slow(1.50s)")
    print("   串行理论 = 0.05+0.30+1.50 = 1.85s | 并发理论 ≈ max = 1.50s")
    print("-" * 66)
    blocks = [tool_use("fast", "tu_f"), tool_use("med", "tu_m"), tool_use("slow", "tu_s")]
    t0 = time.monotonic()
    user_blocks, tool_calls = _execute_tool_blocks(reg, blocks, tool_timeout=10)
    dt = time.monotonic() - t0
    print(f"   实际耗时 = {fmt(dt)}  ({'≈ max，并发生效' if dt < 1.8 else '异常'})")
    for tc in tool_calls:
        print(f"     {tc['name']:<5} 结果 ok={tc['result'].get('name')}")

    print("\n" + "=" * 66)
    print("2) 每工具独立超时：加一个挂死的工具 hung(60s)，tool_timeout=0.5s")
    print("   期望：hung 在 0.5s 被按失败回填，整轮不被拖到 60s")
    print("-" * 66)
    blocks2 = [tool_use("fast", "tu_f"), tool_use("hung", "tu_h"), tool_use("slow", "tu_s")]
    t0 = time.monotonic()
    user_blocks2, tool_calls2 = _execute_tool_blocks(reg, blocks2, tool_timeout=0.5)
    dt2 = time.monotonic() - t0
    print(f"   实际耗时 = {fmt(dt2)}  (远小于 60s，超时生效)")
    for tc in tool_calls2:
        res = tc["result"]
        label = "超时" if res.get("error") == "tool timeout" else "ok"
        print(f"     {tc['name']:<5} -> {label}")

    print("\n" + "=" * 66)
    print("结论：并发把耗时从「和」变「最大值」；独立超时防止单个挂死工具拖垮整轮。")


if __name__ == "__main__":
    main()
