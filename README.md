# Agent Loop — 基于 DeepSeek 原生 API 的最简 ReAct 循环

一个**零第三方依赖**的 AI Agent 示例：通过调用 DeepSeek 官方原生 REST API
（不依赖 OpenAI SDK），实现 ReAct（Reason + Act）模式的 Agent 循环骨架。

模型可以在「直接回答」与「调用工具」之间自主决策：当它决定调用工具时，
程序在本机执行工具并把结果回传给模型，如此反复，直到模型不再要求调用工具为止。

## 特性

- 🧩 **ReAct 循环**：Reason（推理）→ Act（调用工具）→ 观察结果 → 再推理，直到完成
- 📦 **仅用 Python 标准库**：`urllib` / `json` / `inspect` / `subprocess` 等，无需 `pip install`
- 🛠 **装饰器式工具注册**：用 `@tool` 一行即可注册新工具，自动生成 JSON Schema
- 📄 **自动 schema 生成**：根据函数的类型注解（`int/float/str/bool`）与 docstring 自动生成
  `tools` 声明，无需手写 JSON
- 🖥 **内置 6 个开箱即用的工具**：bash、环境诊断、文件读写改查
- 🔒 **路径安全限制**：文件工具只允许在工作目录内操作，防止越权访问系统其它位置
- 📊 **Token 用量打印**：每轮输出输入/输出 token 数，便于观察消耗

## 工作原理（Agent Loop）

```
用户问题 ──┐
          ▼
  ┌────────────────────┐     ┌─────────────────────┐
  │  拼接 messages      │────▶│  DeepSeek           │
  │  (system + 历史)    │     │  /chat/completions  │
  └────────────────────┘     └─────────┬───────────┘
        ▲                              │
        │ 结果拼回 messages             ▼
        │                         是否请求 tool_calls？
        └────────── 否 ────────── 否 │ 是
                                    ▼
                         逐个执行工具(run_tool)
                         结果以 role="tool" 追加历史
```

循环终止条件：

1. 模型本轮回复**不包含** `tool_calls` → 输出最终答案，结束；
2. 达到最大轮数 `max_steps`（默认 20）→ 停止并提示。

## 环境要求

- Python 3.9+（使用了 `list[dict]`、`str | None` 等新式类型注解）
- 一个 DeepSeek API Key（在 https://platform.deepseek.com 申请）
- 网络可访问 `https://api.deepseek.com`

## 快速开始

### 1. 设置环境变量

```bash
# Windows PowerShell
$env:DEEPSEEK_API_KEY = "你的key"

# 可选：切换模型（默认 deepseek-v4-flash）
$env:DEEPSEEK_MODEL = "deepseek-v4-pro"   # 更强推理模型
```

```bash
# Linux / macOS
export DEEPSEEK_API_KEY="你的key"
```

### 2. 运行

```bash
python agent_loop.py
```

程序会提示你输入问题，例如：

```
> 帮我在 demo 目录下创建一个 readme.txt，内容写上当前系统信息
```

Agent 会自动决定调用 `write`、`get_environment` 等工具完成该任务。

## 配置

| 环境变量 | 默认值 | 说明 |
| --- | --- | --- |
| `DEEPSEEK_API_KEY` | （必填） | DeepSeek API 密钥 |
| `DEEPSEEK_MODEL` | `deepseek-v4-flash` | 使用的模型名 |

> ⚠️ 注意：`deepseek-v4-pro` 的 thinking 推理模式**不保证支持函数调用**，
> 做 Agent Loop 时请使用默认的非思考模式，需要工具调用时不要开启 thinking。

## 内置工具

| 工具 | 说明 |
| --- | --- |
| `bash(command)` | 在本地执行 shell 命令并返回输出（Windows 优先 Git Bash，回退 cmd.exe；30 秒超时，输出截断 4000 字符） |
| `get_environment()` | 返回执行环境诊断：操作系统、Python 版本、bash 后端等 |
| `read(path)` | 读取工作目录内的文本文件（单次最多 20000 字符） |
| `write(path, content)` | 写入/创建文件（自动创建父目录，限工作目录内） |
| `edit(path, old, new)` | 将文件中第一处 `old` 替换为 `new` |
| `glob(pattern)` | 按通配符查找文件/目录（支持 `*` `**` `?` `[abc]`，最多返回 100 条） |

## 如何扩展新工具

只需用 `@tool` 装饰一个**带类型注解**的普通函数，Schema 与分发会自动完成：

```python
@tool
def add(a: int, b: int) -> int:
    """计算两个整数之和。"""
    return a + b
```

- 函数名 → 工具名（也可用 `@tool(name="别名")` 改名）
- docstring → 工具描述
- 参数类型注解 → 自动生成的 `parameters` JSON Schema
- 在 `run_tool` 中无需任何改动，自动按名称分发

## 目录结构

```
MyAgent/
├── agent_loop.py        # 主程序：Agent Loop 全部实现
├── demo/
│   └── hello.txt        # 示例工作文件（供 Agent 读写测试）
├── README.md            # 本文件
└── .gitignore           # 忽略 __pycache__ / *.pyc
```

> `_WORK_DIR` 即启动脚本时的当前工作目录（`os.getcwd()`），
> 所有文件工具的读写都被限制在该目录内。

## 代码结构速览

| 模块 / 函数 | 职责 |
| --- | --- |
| `chat_completion()` | 封装 DeepSeek 原生 `/chat/completions` 请求 |
| `@tool` / `_TOOL_REGISTRY` | 装饰器与工具注册表 |
| `generate_tool_schema()` | 由函数注解自动生成 tools 声明 |
| `run_tool()` | 按工具名分发执行，返回 JSON 字符串 |
| `bash` / `get_environment` | 系统类示例工具 |
| `read` / `write` / `edit` / `glob` | 文件类示例工具（带 `_safe_path` 路径校验） |
| `agent_loop()` | 主循环：对话 → 决策 → 执行工具 → 回填历史 |
| `__main__` | 环境变量检查 + 交互式输入入口 |

## 注意事项

- 未设置 `DEEPSEEK_API_KEY` 时，程序会打印指引并以退出码 1 结束
- 工具调用结果以 `role="tool"` 回传时，`tool_call_id` 必须与模型的
  `tool_call.id` 一一对应，否则 API 会报错
- `bash` 工具在 Windows 上会优先探测 Git Bash 真实路径
  （含注册表方式），找不到时回退到系统自带的 `cmd.exe`
