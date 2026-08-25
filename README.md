# Feidudu — 基于 DeepSeek 原生 API 的 ReAct Agent

一个**零第三方依赖**的 AI Agent：**Feidudu**。
通过调用 DeepSeek 官方原生 REST API（不依赖 OpenAI SDK），实现 ReAct（Reason + Act）
模式的 Agent 循环骨架。

模型可以在「直接回答」与「调用工具」之间自主决策：当它决定调用工具时，
程序在本机执行工具并把结果回传给模型，如此反复，直到模型不再要求调用工具为止。

## 特性

- 🧩 **ReAct 循环**：Reason（推理）→ Act（调用工具）→ 观察结果 → 再推理，直到完成
- 📦 **仅用 Python 标准库**：`urllib` / `json` / `inspect` / `subprocess` 等，无需 `pip install`
- 🛠 **装饰器式工具注册**：用 `@tool` 一行即可注册新工具，自动生成 JSON Schema
- 📄 **自动 schema 生成**：根据函数的类型注解（`int/float/str/bool`）与 docstring 自动生成
  `tools` 声明，无需手写 JSON
- 🖥 **内置 12 个开箱即用的工具**：bash、环境诊断、文件读写改查、联网搜索、任务计划、subAgent 委派
- 👥 **subAgent 委派**：主 Agent 可把独立子任务委派给嵌套的子会话执行，结果自动压缩，不污染主上下文
- 🔒 **路径安全限制**：文件工具只允许在工作目录内操作，防止越权访问系统其它位置
- 📊 **Token 用量打印**：每轮输出输入/输出 token 数，便于观察消耗
- 🗂 **任务计划与防漂移**：多步任务可拆分为 step 并跟踪状态，长时间未推进计划时自动注入提醒，防止目标漂移

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
2. 达到单段轮数上限（`AGENT_MAX_STEPS`，默认 20）→ 若任务有计划且未完成，
   自动注入"自动续跑"提醒继续下一段，直到计划完成或达到总硬上限
   （`AGENT_MAX_TOTAL_STEPS` 段，默认 3 段 = 60 轮；设 0 关闭自动续跑）。

> 无活跃计划时，单段上限即硬上限，用尽即停。`20` 限制的是"一条用户消息"，
> 跨多条消息的长任务每条消息都重新获得单段预算。

## 环境要求

- Python 3.10+（使用了 `list[dict]`、`str | None` 等新式类型注解，`str | None` 为 PEP 604 语法）
- 一个 DeepSeek API Key（在 https://platform.deepseek.com 申请）
- 网络可访问 `https://api.deepseek.com`

## 快速开始

### 1. 设置 API Key（二选一）

**方式 A：环境变量**

```bash
# Windows PowerShell
$env:DEEPSEEK_API_KEY = "你的key"

# Linux / macOS
export DEEPSEEK_API_KEY="你的key"
```

**方式 B：本地 .env 文件（推荐，已被 git 忽略）**

在本项目目录创建 `.env`（也可直接复制 `.env.example` 填写）：

```
DEEPSEEK_API_KEY=你的key
DEEPSEEK_MODEL=deepseek-v4-pro   # 可选：切换模型（默认 deepseek-v4-flash）
BOCHA_API_KEY=你的博查key         # 可选：websearch 工具需要（https://open.bocha.cn）
```

两者任一即可，同时存在时已设置的环境变量优先。

### 2. 安装为命令（推荐）

```bash
pip install -e . textual
feidudu          # 直接启动聊天式 TUI
```

### 3. 运行

```bash
python main.py            # 终端交互模式
python main.py --tui      # 聊天式 TUI（同 feidudu）
```

程序会提示你输入问题，例如：

```
> 帮我在 demo 目录下创建一个 readme.txt，内容写上当前系统信息
```

### 4. 聊天式 TUI

`feidudu` 默认启动全屏聊天式 TUI（类似 Claude Code 的交互），也可以 `python main.py --tui`：

- 底部输入框，回车发送；上方滚动消息区实时展示用户 / Agent / 工具调用 / subAgent 委派
- **启动时显示 res/feidudu.png 图片 Logo**（Pillow 转 ANSI 真彩色渲染；未装 Pillow 或图片缺失时回退 ASCII 图腾）
- 工具需要权限确认时，输入框上方弹出"允许 / 拒绝"按钮
- `/exit` 或 `/quit` 退出，`/clear` 清屏

> TUI 是唯一需要第三方依赖（textual / pillow）的功能；纯终端模式仍保持零依赖。
> 实现走 `core/output.py` 统一输出通道：终端默认 print，TUI 注入 sink 把输出送进界面，核心行为两者一致。

Agent 会自动决定调用 `write`、`get_environment` 等工具完成该任务。

## 配置

| 环境变量 | 默认值 | 说明 |
| --- | --- | --- |
| `DEEPSEEK_API_KEY` | （必填） | DeepSeek API 密钥 |
| `DEEPSEEK_MODEL` | `deepseek-v4-flash` | 使用的模型名 |
| `BOCHA_API_KEY` | （websearch 需要） | 博查搜索 API 密钥（https://open.bocha.cn） |
| `BOCHA_BASE_URL` | `https://api.bocha.cn/v1` | 博查 API 地址（一般无需修改） |
| `AGENT_MAX_STEPS` | `20` | 每条用户消息的单段轮数预算 |
| `AGENT_MAX_TOTAL_STEPS` | `3` | 自动续跑段数（总硬上限 = 单段预算 × 段数；设 `0` 关闭自动续跑） |
| `AGENT_DEBUG` | 关闭 | 钩子调用标记调试日志（设 `1` / `true` / `yes` / `on` 开启） |

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
| `websearch(query, count)` | 联网搜索，返回标题/链接/摘要（基于博查 API，需 `BOCHA_API_KEY`） |
| `subagent(task, name)` | 把独立子任务委派给一个嵌套 subAgent 执行，返回压缩后的最终结果 |
| `plan_task(steps)` | 把多步骤任务拆分为 step 列表，创建/替换当前任务计划 |
| `update_step(step_index, status, note, reason)` | 更新某个 step 的状态（未执行/执行中/执行完成/执行失败/跳过），系统强制校验转移合法性 |
| `revise_plan(steps)` | 整体修订计划：新增 / 删除 / 重排 step |
| `get_plan()` | 查看当前计划各 step 的状态与说明 |

### subAgent 委派

主 Agent 可把**独立、可隔离**的子任务（查询资料、处理单个文件、独立计算等）委派给 subAgent：

- subAgent 内部就是一个 `AgentSession`：复用同一个主循环与同一套全局钩子，
  权限校验（deny / confirm）在子会话里照样生效。
- subAgent **不能**使用 4 个计划工具（拆解由主 Agent 做），也**不能**再调用 subAgent，
  防止无限递归（嵌套深度固定为 1）。
- subAgent 的中间过程不会污染主上下文，只把最终结果返回给主 Agent。
- **结果压缩**：结果超过 2000 字符时，先让 subAgent 用自己的模型能力总结（最多 2 次），
  仍超长才截断兜底，避免截断丢失关键信息。
- 主 Agent 调用时应给子会话起一个贴合职责的英文小写短横线名字（如 `web-researcher`），
  该名字会作为该 subAgent 所有控制台输出的前缀。

### 任务计划与防目标漂移

多步任务执行时，模型可先调用 `plan_task` 把任务拆成 step，再逐步执行：

- **五态跟踪**：`未执行 → 执行中 → 执行完成 / 执行失败 / 跳过`，系统在 `update_step`
  中强制校验转移合法性——不允许跳步、不允许同时推进多个 step、"执行完成"必须带
  说明、执行失败必须带原因，防止模型"假完成"。
- **顺序推进**：只有当前 step 终结后才允许推进下一个（顺序前沿）。
- **防漂移提醒**：计划活跃期间，若连续 6 轮模型调用都没推进计划，
  循环会在下次请求前自动注入一条紧凑计划快照提醒，把模型拉回任务主线；
  每轮用户输入前也会注入当前计划摘要，便于"继续 / 还差什么"这类追问。
- **重试上限**：单个 step 最多重试 3 次，之后只能跳过、失败或修订计划。
- **生命周期**：计划挂在会话状态上，`/clear` 会自动丢弃。

> 计划工具是纯内存操作、无破坏性，权限层直接放行（不弹确认）。
> 单步即可完成的任务不需要规划——是否拆解由模型自行判断。

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

新增工具模块后，在 `tools/__init__.py` 里加一行 import 即可被 Agent 使用。

## 目录结构

```
MyAgent/
├── main.py               # 入口：加载 .env → 注册工具/钩子 → 启动对话（feidudu 命令）
├── config.py             # .env 加载 + API 常量
├── chat_tui.py           # 聊天式 TUI（textual）
├── SOUL.md               # 人设系统提示词（Agent 启动时作为 system prompt 注入）
├── pyproject.toml        # 打包配置：声明 feidudu 命令入口
├── README.md             # 本文件
├── .env.example          # 环境变量配置示例（复制为 .env 使用）
├── .gitignore            # 忽略 __pycache__ / *.pyc / .env / egg-info
├── res/
│   └── feidudu.png       # 黄色袋鼠 Logo（TUI 启动时以 ANSI 真彩色渲染）
├── core/                 # Agent 核心框架
│   ├── __init__.py
│   ├── llm.py            # DeepSeek REST 通信（chat_completion）
│   ├── tools.py          # @tool 注册表 + 白名单 + schema + run_tool
│   ├── hooks.py          # HookContext + HookRegistry + 单例 hooks
│   ├── planner.py        # 任务计划状态机（纯函数）
│   ├── output.py         # 统一输出通道（终端 print / TUI 注入）
│   ├── banner.py         # ASCII 启动横幅（回退用）
│   ├── logo.py           # 图片版启动 Logo（Pillow 绘制 + ANSI 渲染）
│   └── loop.py           # agent_loop 主循环 + AgentSession（钩子驱动）
├── tools/                # 内置工具与钩子
│   ├── __init__.py       # 统一 import 触发注册
│   ├── bash_tool.py      # bash + Git Bash 探测
│   ├── env_tool.py       # get_environment
│   ├── file_tools.py     # read/write/edit/glob + _safe_path
│   ├── planner_tools.py  # plan_task/update_step/revise_plan/get_plan
│   ├── subagent_tool.py  # subagent（嵌套子会话委派）
│   ├── websearch_tool.py # websearch（博查 API）
│   └── hooks_setup.py    # 权限钩子 + 示例钩子 + 计划钩子 + 注册
└── tests/                # 单元测试（pytest）
    ├── test_file_tools.py
    ├── test_hooks.py
    ├── test_llm.py
    ├── test_permission.py
    ├── test_planner.py
    ├── test_subagent.py
    ├── test_tools.py
    └── test_chat_tui.py
```

> `_WORK_DIR` 即启动脚本时的当前工作目录（`os.getcwd()`），
> 所有文件工具的读写都被限制在该目录内。

## 代码结构速览

| 模块 / 函数 | 职责 |
| --- | --- |
| `core/llm.py` `chat_completion()` | 封装 DeepSeek 原生 `/chat/completions` 请求 |
| `core/tools.py` `@tool` / `_TOOL_REGISTRY` | 装饰器与工具注册表（注册即入白名单） |
| `core/tools.py` `generate_tool_schema()` | 由函数注解自动生成 tools 声明 |
| `core/tools.py` `run_tool()` | 按工具名分发执行，返回 JSON 字符串 |
| `core/hooks.py` | HookContext / HookRegistry / 6 个事件定义 |
| `core/planner.py` | 任务计划状态机：五态转移校验、序列化、修订（纯函数） |
| `core/output.py` | 统一输出通道：终端 print / TUI sink 注入 |
| `core/loop.py` `AgentSession` / `agent_loop()` | 会话与主循环：对话 → 钩子裁决 → 执行工具 → 回填历史 |
| `chat_tui.py` | 聊天式 TUI：全屏消息流 + 底部输入 + 权限确认按钮 |
| `tools/bash_tool.py` `bash` | 执行 shell 命令（Git Bash → cmd.exe 回退） |
| `tools/env_tool.py` `get_environment` | 环境诊断 |
| `tools/file_tools.py` `read/write/edit/glob` | 文件读写改查（带 `_safe_path` 路径校验） |
| `tools/planner_tools.py` `plan_task` 等 | 计划拆分 / 状态更新 / 修订 / 查询 |
| `tools/subagent_tool.py` `subagent` | 嵌套子会话：委派独立子任务，超长结果自动总结/截断 |
| `tools/hooks_setup.py` `_permission_check` | 权限钩子：危险操作 deny / 需确认 confirm / 无害 allow |
| `main.py` | 环境变量检查 + 交互式输入入口 |

## 注意事项

- 未设置 `DEEPSEEK_API_KEY` 时，程序会打印指引并以退出码 1 结束
- 工具调用结果以 `role="tool"` 回传时，`tool_call_id` 必须与模型的
  `tool_call.id` 一一对应，否则 API 会报错
- `bash` 工具在 Windows 上会优先探测 Git Bash 真实路径
  （含注册表方式），找不到时回退到系统自带的 `cmd.exe`
