# Agent 学习 · 工具调用与编排循环

用理财看板项目做渐进式 Agent 学习的第一课产出。目标：应聘 **Agent 工程师**，补"亲自写 Agent 编排 + 可面试作品"的缺口。

## 内容

| 文件 | 作用 |
|---|---|
| `agent_tools.py` | 通用工具注册表：`Tool`（name/description/JSON Schema/executor）+ `ToolRegistry`，可导出 Anthropic `tools` 数组 |
| `llm_client.py` | 裸 HTTP 调 DeepSeek **Anthropic 兼容接口**（零依赖）；`run_tool_loop` 编排循环骨架；离线 `MockLLM` 兜底 |
| `news_classifier.py` | 用 `tool_choice` 强制"分类工具"做结构化输出，替换正则分类；真模型→mock→正则三级兜底 |
| `demo_classify.py` | 正则 vs LLM 分类对比演示 |
| `agent学习笔记_day1_工具调用.md` | 第 1 天学习笔记（含面试题卡） |

## 运行

```bash
# 配置（复制模板后填 key；不填也能跑，会用 MockLLM 离线兜底）
cp config.example.json config.json   # 在 config.json 里填 api_key

# 演示：真实模型
python3 demo_classify.py
# 演示：离线 mock
python3 demo_classify.py --mock
```

## 技术要点

- LLM：`deepseek-chat`（`deepseek-reasoner` 不支持 tools）
- 接口：`https://api.deepseek.com/anthropic/v1/messages`，`x-api-key` 认证，Anthropic Messages 格式
- `run_tool_loop` = Agent 核心循环：发请求 → 模型返回 tool_use → 执行工具 → 回填 tool_result → 再问，直到收敛；`max_steps` 安全阀 + 超限兜底

> 隐私说明：`config.json` 含 API 密钥，已 gitignore，**不入库**。
