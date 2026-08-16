#!/usr/bin/env python3
"""Agent 第一课：通用工具注册表。

把任意"能执行任务的函数"包装成标准工具（name + description + JSON Schema 参数 + executor），
注册进 ToolRegistry，并可导出成 Anthropic / OpenAI 兼容的 tools 数组，供任何支持
function-calling / tool-call 的模型消费。这是后续 Agent 编排与 MCP 落地的地基。

概念映射：
- 一个 Tool ≈ 一个函数 + 它的"说明书"(schema) + 执行器。
- ToolRegistry ≈ 工具清单，负责注册、查询、按 name 派发执行。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional


@dataclass
class Tool:
    """一个可被 LLM 调用的工具。"""

    name: str
    description: str
    input_schema: Dict[str, Any]
    executor: Callable[..., Any]

    def execute(self, args: Optional[Dict[str, Any]] = None) -> Any:
        """调用真实执行器，返回结果（建议返回可 JSON 序列化的 dict/str）。"""
        return self.executor(**(args or {}))

    def as_anthropic(self) -> Dict[str, Any]:
        """导出为 Anthropic Messages API 的 tool 定义。"""
        return {
            "name": self.name,
            "description": self.description,
            "input_schema": self.input_schema,
        }

    def __repr__(self) -> str:
        return f"<Tool {self.name}>"


class ToolRegistry:
    """工具的注册表 + 派发器。"""

    def __init__(self) -> None:
        self._tools: Dict[str, Tool] = {}

    def register(self, tool: Tool) -> "ToolRegistry":
        if tool.name in self._tools:
            raise ValueError(f"Tool already registered: {tool.name}")
        self._tools[tool.name] = tool
        return self

    def register_func(
        self,
        name: str,
        description: str,
        input_schema: Dict[str, Any],
    ) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
        """装饰器形式注册：把普通函数包装成 Tool。"""

        def decorator(fn: Callable[..., Any]) -> Callable[..., Any]:
            self.register(Tool(name, description, input_schema, fn))
            return fn

        return decorator

    def get(self, name: str) -> Optional[Tool]:
        return self._tools.get(name)

    def list(self) -> List[Tool]:
        return list(self._tools.values())

    def names(self) -> List[str]:
        return list(self._tools.keys())

    def as_anthropic_tools(self) -> List[Dict[str, Any]]:
        """导出成 Anthropic tools 数组，可直接放进 /v1/messages 的 tools 字段。"""
        return [t.as_anthropic() for t in self._tools.values()]

    def execute(self, name: str, args: Optional[Dict[str, Any]] = None) -> Any:
        """按 name 派发执行；未注册抛 KeyError。"""
        tool = self._tools.get(name)
        if tool is None:
            raise KeyError(f"Unknown tool: {name}")
        return tool.execute(args)

    def __contains__(self, name: str) -> bool:
        return name in self._tools


def _j(value: Any) -> str:
    """供 CLI/调试打印结果。"""
    return json.dumps(value, ensure_ascii=False, indent=2)


if __name__ == "__main__":
    # 简单自测
    reg = ToolRegistry()

    @reg.register_func(
        "echo",
        "原样返回输入，用于调试。",
        {
            "type": "object",
            "properties": {"text": {"type": "string", "description": "要回显的文字"}},
            "required": ["text"],
        },
    )
    def _echo(text: str) -> dict:
        return {"text": text}

    print(json.dumps(reg.as_anthropic_tools(), ensure_ascii=False, indent=2))
    print(_j(reg.execute("echo", {"text": "hello agent"})))
