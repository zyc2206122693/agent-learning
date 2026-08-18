#!/usr/bin/env python3
"""Agent 第一课：LLM 客户端（裸 HTTP，零依赖）。

调 DeepSeek 的 Anthropic 兼容接口（POST {base_url}/v1/messages），
支持 system / messages / tools / tool_choice，解析 text 与 tool_use block，
并提供一个可复用的「工具调用循环」run_tool_loop —— 这正是 Agent 编排的核心骨架：
  发请求 → 若返回 tool_use → 执行工具 → 把 tool_result 回填 → 再发一轮。

- LLMClient  : 真实调用（无密钥/网络失败时抛 LLMError，由调用方兜底）。
- MockLLM    : 离线确定性实现，输出与真模型同构，保证不依赖密钥也能跑通整条链路。
- 配置读取   : 环境变量 DEEPSEEK_API_KEY / ANTHROPIC_API_KEY 优先，其次 config.json。
- 安全       : API key 只在内存/config 里，日志一律打码，绝不打印明文。
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(BASE_DIR, "config.json")

DEFAULT_BASE_URL = "https://api.deepseek.com/anthropic"
DEFAULT_MODEL = "deepseek-chat"
ANTHROPIC_VERSION = "2023-06-01"

# 单次对话允许的工具调用轮数上限，防止死循环。
MAX_TOOL_STEPS = 6

# 每个工具的独立超时（秒）：超过则按失败回填，让模型带已有结果继续，不等死。
DEFAULT_TOOL_TIMEOUT = 30.0


def _execute_tool_blocks(registry, tool_use_blocks, tool_timeout: float = DEFAULT_TOOL_TIMEOUT):
    """并发执行一组 tool_use（每个工具独立超时），返回 (user_blocks, tool_calls)。

    第 1 课 Q4 的落地：把串行 for（耗时=sum）升级为并发（耗时=max）。
    - 每个工具用一个 worker 线程，`fut.result(timeout)` 给独立超时，超时按失败回填；
    - `executor.shutdown(wait=False)` 不等待卡住的线程 → 真正 fail-fast，不会因一个
      挂死的工具拖垮整轮。
    注意：卡死的工具线程会留在后台（生产环境可换进程边界隔离），但不会再阻塞主循环。
    """
    from concurrent.futures import ThreadPoolExecutor
    import time

    results: Dict[int, Any] = {}

    def run(idx: int, block: Dict[str, Any]):
        name, args, tid = block.get("name"), block.get("input", {}), block.get("id")
        try:
            res = registry.execute(name, args)
            return (idx,
                    {"type": "tool_result", "tool_use_id": tid,
                     "content": json.dumps(res, ensure_ascii=False, default=str), "is_error": False},
                    {"name": name, "args": args, "result": res})
        except Exception as e:
            return (idx,
                    {"type": "tool_result", "tool_use_id": tid,
                     "content": json.dumps({"error": str(e)}, ensure_ascii=False, default=str), "is_error": True},
                    {"name": name, "args": args, "result": {"error": str(e)}})

    executor = ThreadPoolExecutor(max_workers=len(tool_use_blocks))
    try:
        futures = [executor.submit(run, i, b) for i, b in enumerate(tool_use_blocks)]
        for i, fut in enumerate(futures):
            try:
                idx, ub, tc = fut.result(timeout=tool_timeout)
                results[idx] = (ub, tc)
            except Exception:  # 超时或执行器异常 → 按失败回填
                block = tool_use_blocks[i]
                tid, name = block.get("id"), block.get("name")
                ub = {"type": "tool_result", "tool_use_id": tid,
                      "content": json.dumps({"error": f"tool timeout after {tool_timeout}s"}, ensure_ascii=False),
                      "is_error": True}
                tc = {"name": name, "args": block.get("input", {}), "result": {"error": "tool timeout"}}
                results[i] = (ub, tc)
    finally:
        executor.shutdown(wait=False)  # 不等待卡死的线程，实现真正的 fail-fast

    order = sorted(results.keys())  # 按原顺序稳定输出，便于回放
    return [results[i][0] for i in order], [results[i][1] for i in order]


class LLMError(Exception):
    """LLM 调用失败（无密钥 / 网络 / 上游错误）。调用方据此回退到兜底策略。"""


@dataclass
class LLMConfig:
    api_key: str = ""
    base_url: str = DEFAULT_BASE_URL
    model: str = DEFAULT_MODEL
    timeout_seconds: float = 30.0

    @classmethod
    def load(cls, path: Optional[str] = None) -> "LLMConfig":
        path = path or CONFIG_PATH
        file_cfg: Dict[str, Any] = {}
        if os.path.exists(path):
            try:
                with open(path, "r", encoding="utf-8") as f:
                    file_cfg = json.load(f)
            except Exception:
                file_cfg = {}
        api_key = (
            os.environ.get("DEEPSEEK_API_KEY")
            or os.environ.get("ANTHROPIC_API_KEY")
            or file_cfg.get("api_key", "")
        )
        return cls(
            api_key=api_key,
            base_url=file_cfg.get("base_url", DEFAULT_BASE_URL),
            model=file_cfg.get("model", DEFAULT_MODEL),
            timeout_seconds=float(file_cfg.get("timeout_seconds", 30.0)),
        )


def mask_key(key: str) -> str:
    """日志里只显示后 4 位，避免泄露。"""
    if not key:
        return "(empty)"
    return "****" + key[-4:]


class LLMClient:
    """真实 DeepSeek Anthropic 兼容接口客户端。"""

    def __init__(self, config: Optional[LLMConfig] = None) -> None:
        self.config = config or LLMConfig.load()
        self.url = self.config.base_url.rstrip("/") + "/v1/messages"

    @property
    def ready(self) -> bool:
        return bool(self.config.api_key)

    def _headers(self) -> Dict[str, str]:
        return {
            "Content-Type": "application/json",
            "x-api-key": self.config.api_key,
            "anthropic-version": ANTHROPIC_VERSION,
        }

    def complete(
        self,
        system: Optional[str] = None,
        messages: Optional[List[Dict[str, Any]]] = None,
        tools: Optional[List[Dict[str, Any]]] = None,
        tool_choice: Optional[Any] = None,
        model: Optional[str] = None,
        max_tokens: int = 1024,
        temperature: float = 0.0,
    ) -> Dict[str, Any]:
        """发一次非流式请求，返回解析后的 Anthropic 响应 dict。

        messages: [{"role": "user"|"assistant", "content": str | [block, ...]}]
        """
        if not self.ready:
            raise LLMError("API key 未配置（env 或 config.json）")

        payload: Dict[str, Any] = {
            "model": model or self.config.model,
            "max_tokens": max_tokens,
            "temperature": temperature,
        }
        if system:
            payload["system"] = system
        if messages:
            payload["messages"] = self._wire_messages(messages)
        if tools:
            payload["tools"] = tools
        if tool_choice is not None:
            payload["tool_choice"] = tool_choice

        data = self._post(payload)
        if "content" not in data:
            raise LLMError(f"响应缺少 content: {str(data)[:200]}")
        return data

    def _post(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        req = urllib.request.Request(
            self.url,
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers=self._headers(),
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=self.config.timeout_seconds) as resp:
                return json.loads(resp.read().decode("utf-8", errors="replace"))
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", errors="replace")
            raise LLMError(f"HTTP {e.code}: {body[:300]}") from e
        except urllib.error.URLError as e:
            raise LLMError(f"网络错误: {e.reason}") from e
        except Exception as e:
            raise LLMError(f"请求异常: {e}") from e

    @staticmethod
    def _wire_messages(messages: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """把 (role, content) 归一化成 Anthropic 需要的 content 为 block 列表。"""
        out = []
        for m in messages:
            content = m.get("content")
            if isinstance(content, str):
                content = [{"type": "text", "text": content}]
            out.append({"role": m.get("role", "user"), "content": content})
        return out

    def run_tool_loop(
        self,
        registry,
        system: Optional[str] = None,
        messages: Optional[List[Dict[str, Any]]] = None,
        tool_choice: Optional[Any] = None,
        model: Optional[str] = None,
        max_tokens: int = 1024,
        max_steps: int = MAX_TOOL_STEPS,
        tool_timeout: float = DEFAULT_TOOL_TIMEOUT,
    ) -> Dict[str, Any]:
        """Agent 编排核心骨架：LLM 决定调哪个工具 → 并发执行 → 结果回填，循环直到完成。

        registry: ToolRegistry 实例，用于 as_anthropic_tools() 和 execute()。
        tool_timeout: 每个工具的独立超时秒数（并发执行，超过按失败回填）。
        返回 {final_text, transcript, steps, tool_calls}。
        """
        tools = registry.as_anthropic_tools()
        messages = list(messages or [])
        transcript: List[Dict[str, Any]] = []
        tool_calls: List[Dict[str, Any]] = []

        for _ in range(max_steps):
            resp = self.complete(
                system=system,
                messages=messages,
                tools=tools,
                tool_choice=tool_choice,
                model=model,
                max_tokens=max_tokens,
            )
            blocks = resp.get("content", [])
            tool_use_blocks = [b for b in blocks if b.get("type") == "tool_use"]

            # 1) 把这一轮 assistant 回复追加进对话历史
            messages.append({"role": "assistant", "content": blocks})
            transcript.append({"assistant": blocks})

            # 2) 没有工具调用 → 收尾，返回最终文本
            if not tool_use_blocks:
                final_text = "".join(b.get("text", "") for b in blocks if b.get("type") == "text")
                return {
                    "final_text": final_text,
                    "transcript": transcript,
                    "steps": len(transcript),
                    "tool_calls": tool_calls,
                }

            # 3) 有工具调用 → 并发执行（每工具独立超时），结果以 user 角色 + tool_result 回填
            user_blocks, tool_calls_this = _execute_tool_blocks(registry, tool_use_blocks, tool_timeout)
            tool_calls.extend(tool_calls_this)
            messages.append({"role": "user", "content": user_blocks})
            transcript.append({"user(tool_results)": user_blocks})

        # 达到步数上限仍未收敛
        final_text = "".join(
            b.get("text", "")
            for m in messages
            for b in (m.get("content", []) if isinstance(m.get("content"), list) else [])
            if isinstance(b, dict) and b.get("type") == "text"
        )
        return {
            "final_text": final_text,
            "transcript": transcript,
            "steps": len(transcript),
            "tool_calls": tool_calls,
            "truncated": True,
        }


class MockLLM:
    """离线确定性 LLM，输出与 Anthropic 响应同构，用于无密钥/无网络时演示整条链路。

    它模拟"模型返回一个 tool_use block"：找到注册表里的分类工具，用简单启发式
    填出结构化 input，从而让 demo 在离线也能跑出和真模型相同的调用形态。
    """

    def __init__(self, config: Optional[LLMConfig] = None) -> None:
        self.config = config or LLMConfig.load()

    @property
    def ready(self) -> bool:
        return True  # mock 永远可用

    def _classify_heuristic(self, text: str) -> Dict[str, Any]:
        """极简启发式，仅让 mock 输出结构合法、可读，不追求准确。"""
        category = "市场"
        for name, pat in (("宏观", "央行|美联储|加息|降息|CPI|PPI|GDP|经济"), ("行业", "芯片|半导体|AI|机器人|新能源|汽车|黄金|石油"), ("公司", "公告|收购|减持|增持|业绩|回购|中标")):
            import re
            if re.search(pat, text):
                category = name
                break
        sentiment = "中性"
        if re.search("跌停|大跌|下跌|减持|亏损|风险|暴跌", text):
            sentiment = "利空"
        elif re.search("涨停|大涨|上涨|增持|超预期|新高|增长|中标|获批", text):
            sentiment = "利好"
        related = []
        import re
        for label, pat in (("纳指", "纳斯达克|纳指"), ("AI 主题", "人工智能|AI|算力"), ("机器人", "机器人"), ("制造业", "半导体|制造"), ("资源", "资源|有色|港股"), ("债券", "债券|债市")):
            if re.search(pat, text):
                related.append(label)
        return {"category": category, "sentiment": sentiment, "related": related}

    @staticmethod
    def _has_tool_result(messages: List[Dict[str, Any]]) -> bool:
        """对话里是否已存在 tool_result（即已执行过一轮工具）。"""
        for m in messages:
            c = m.get("content")
            if isinstance(c, list):
                for b in c:
                    if isinstance(b, dict) and b.get("type") == "tool_result":
                        return True
        return False

    def complete(self, **kwargs):
        """返回一个与 Anthropic 响应同构的 dict。

        模拟真实 Agent 行为：第一轮返回一个 tool_use（调用第一个工具），
        一旦收到 tool_result 就返回 text（模拟"拿到结果后作答"），从而让
        离线 demo 能正常收敛，而不是无限空转。
        """
        tools = kwargs.get("tools") or []
        messages = kwargs.get("messages") or []
        user_text = self._last_user_text(messages)
        # 已执行过工具 → 模拟模型拿到结果后直接作答，结束循环
        if self._has_tool_result(messages):
            return {
                "content": [{"type": "text", "text": f"(mock) 已根据工具结果作答：关于“{user_text[:40]}”。离线演示模式，真实分类/回答请用真实模型。"}],
                "stop_reason": "end_turn",
            }
        # 找到要演示的分类工具（约定名带 classify / classify_news），否则取第一个工具
        target = None
        for t in tools:
            if "classify" in t.get("name", ""):
                target = t
                break
        if target is None:
            target = tools[0] if tools else None
        if target is None:
            return {"content": [{"type": "text", "text": "mock: 无工具"}]}
        # 分类工具用启发式填枚举；路由工具按关键词判意图；其他工具按 schema 填合法参数
        name = target.get("name", "")
        if "classify" in name:
            tool_input = self._classify_heuristic(user_text)
        elif "route" in name or "intent" in name:
            tool_input = {"intents": self._route_heuristic(user_text)}
        else:
            tool_input = self._mock_args(target)
        return {
            "content": [
                {"type": "tool_use", "id": "mock_tool_use_1", "name": target["name"], "input": tool_input},
            ],
            "stop_reason": "tool_use",
        }

    @staticmethod
    def _route_heuristic(text: str) -> List[str]:
        """mock 用的意图启发式：据关键词判断需要持仓/新闻。"""
        news_kw = any(k in text for k in ["新闻", "行情", "快讯", "动态", "走势", "涨跌", "关注"])
        hold_kw = any(k in text for k in ["持仓", "组合", "基金", "市值", "收益", "加仓", "减仓", "值多少"])
        if news_kw and hold_kw:
            return ["holdings", "news"]
        if news_kw:
            return ["news"]
        return ["holdings"]

    @staticmethod
    def _mock_args(tool_def: Dict[str, Any]) -> Dict[str, Any]:
        """按工具 input_schema 的 required 生成合法示例参数，让 mock 能真实执行工具。"""
        schema = tool_def.get("input_schema") or {}
        required = schema.get("required", [])
        props = schema.get("properties", {})
        args = {}
        for name in required:
            p = props.get(name, {})
            if p.get("enum"):
                args[name] = p["enum"][0]
            elif p.get("type") == "integer":
                args[name] = 1
            elif p.get("type") == "number":
                args[name] = 1.0
            else:
                args[name] = "x"
        return args

    def run_tool_loop(self, registry, system=None, messages=None, **kwargs):
        """为 mock 提供与 LLMClient 相同的接口，驱动工具循环。"""
        tools = registry.as_anthropic_tools()
        messages = list(messages or [])
        transcript, tool_calls = [], []

        for _ in range(kwargs.get("max_steps", MAX_TOOL_STEPS)):
            resp = self.complete(tools=tools, messages=messages, system=system)
            blocks = resp.get("content", [])
            tool_use_blocks = [b for b in blocks if b.get("type") == "tool_use"]
            messages.append({"role": "assistant", "content": blocks})
            transcript.append({"assistant": blocks})
            if not tool_use_blocks:
                final_text = "".join(b.get("text", "") for b in blocks if b.get("type") == "text")
                return {"final_text": final_text, "transcript": transcript, "steps": len(transcript), "tool_calls": tool_calls}
            user_blocks, tool_calls_this = _execute_tool_blocks(registry, tool_use_blocks, DEFAULT_TOOL_TIMEOUT)
            tool_calls.extend(tool_calls_this)
            messages.append({"role": "user", "content": user_blocks})
            transcript.append({"user(tool_results)": user_blocks})

        return {"final_text": "", "transcript": transcript, "steps": len(transcript), "tool_calls": tool_calls, "truncated": True}

    @staticmethod
    def _last_user_text(messages: List[Dict[str, Any]]) -> str:
        for m in reversed(messages):
            if m.get("role") == "user":
                c = m.get("content")
                if isinstance(c, str):
                    return c
                if isinstance(c, list):
                    parts = [x.get("text", "") for x in c if isinstance(x, dict) and x.get("type") == "text"]
                    if parts:
                        return "".join(parts)
        return ""


def make_client(config: Optional[LLMConfig] = None) -> Any:
    """按配置返回真实客户端或 mock。用 use_llm / api_key 决定。"""
    cfg = config or LLMConfig.load()
    use_llm = os.environ.get("USE_LLM", "")
    if use_llm in ("0", "false", "off"):
        return MockLLM(cfg)
    # 无密钥则回退 mock，保证链路可跑
    if cfg.api_key:
        return LLMClient(cfg)
    return MockLLM(cfg)
