#!/usr/bin/env python3
"""
最小的 Agent Loop —— 使用 DeepSeek 原生 REST API（不依赖 OpenAI SDK）。

官方原生接口（https://api-docs.deepseek.com/）：
    POST https://api.deepseek.com/chat/completions
    Authorization: Bearer $DEEPSEEK_API_KEY

这个循环演示 ReAct(Reason + Act) 模式的最简骨架：
  1. 将用户问题 + 历史消息以原生 JSON 发给 DeepSeek
  2. 模型决定是"直接回答"还是"调用工具"(tool_calls)
  3. 执行工具 → 把 tool 结果拼回 messages → 回到第 1 步
  4. 模型不再请求调用工具时，循环结束

运行前提：
  设置环境变量 DEEPSEEK_API_KEY（在 https://platform.deepseek.com 申请）。
  可选 DEEPSEEK_MODEL，默认 deepseek-v4-flash。
  仅用 Python 标准库，无需 pip install 任何东西。

用法：
  python agent_loop.py
"""

import inspect
import json
import os
import platform
import shutil
import subprocess
import sys
import urllib.error
import urllib.request
from typing import get_type_hints

# ----------------------------------------------------------------------
# 配置：原生接口，模型用 DeepSeek 当前正式命名（v4 系列）
# ----------------------------------------------------------------------
DEEPSEEK_API_KEY = os.environ.get("DEEPSEEK_API_KEY")
DEEPSEEK_MODEL = os.environ.get("DEEPSEEK_MODEL", "deepseek-v4-flash")
BASE_URL = "https://api.deepseek.com"
CHAT_URL = f"{BASE_URL}/chat/completions"

# 注意：thinking 思考模式(deepseek-v4-pro 的推理模式)不保证支持函数调用，
# 做 Agent Loop 用默认的非思考模式即可，需要工具调用时不要开 thinking。


def chat_completion(messages: list[dict], tools: list[dict] | None = None) -> dict:
    """调用 DeepSeek 原生 /chat/completions 端点，返回解析后的 JSON。"""
    payload: dict = {
        "model": DEEPSEEK_MODEL,
        "messages": messages,
        "stream": False,
    }
    if tools:
        payload["tools"] = tools

    request = urllib.request.Request(
        CHAT_URL,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {DEEPSEEK_API_KEY}",
        },
        method="POST",
    )

    try:
        with urllib.request.urlopen(request) as response:
            body = response.read().decode("utf-8")
            return json.loads(body)
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"DeepSeek API 返回 {e.code}: {detail}") from e


# ----------------------------------------------------------------------
# 工具注册表：@tool 装饰器 + 自动生成 schema，扩展只需写普通函数
# ----------------------------------------------------------------------
# 注册规则（由装饰器自动完成，无需手动填 JSON）：
#   函数名        -> 工具名
#   函数 docstring -> 工具描述（可选）
#   参数的类型注解 -> parameters schema（必填）
_TOOL_REGISTRY: dict[str, callable] = {}


def tool(func=None, *, name: str | None = None):
    """把被装饰的函数注册为 Agent 可用工具。

    支持两种写法：@tool  或  @tool()  或  @tool(name="别名")
    函数需写参数类型注解（int/float/str/bool），并尽量写一句 docstring
    作为工具描述。
    """

    def decorator(f):
        fname = name or f.__name__
        f._tool_name = fname  # 标记，供自动生成 schema 时识别
        _TOOL_REGISTRY[fname] = f
        return f

    if func is not None:
        return decorator(func)  # @tool 直接用在函数上
    return decorator           # @tool() 或 @tool(name=...) 形式


def _type_to_json(t: type) -> str:
    """把 Python 类型映射为 JSON Schema 类型。"""
    return {
        int: "integer",
        float: "number",
        str: "string",
        bool: "boolean",
    }.get(t, "string")


def generate_tool_schema(func) -> dict:
    """根据注册函数的参数注解与 docstring 自动生成 tools 声明。"""
    hints = get_type_hints(func)
    # 只取参数，排除 'return'（返回值注解不属于入参）
    hints.pop("return", None)

    properties = {}
    required = []
    for param_name, t in hints.items():
        properties[param_name] = {"type": _type_to_json(t)}
        required.append(param_name)

    description = (inspect.getdoc(func) or "").strip()

    return {
        "type": "function",
        "function": {
            "name": getattr(func, "_tool_name", func.__name__),
            "description": description,
            "parameters": {
                "type": "object",
                "properties": properties,
                "required": required,
            },
        },
    }


def registered_tools() -> list[dict]:
    """返回当前注册的全部工具对应的 tools 声明。"""
    return [generate_tool_schema(f) for f in _TOOL_REGISTRY.values()]


def run_tool(name: str, arguments: str) -> str:
    """按工具名分发执行注册的函数，返回 JSON 字符串结果。

    以后加新工具：只要用 @tool 注册即可，这里无需再改。
    """
    func = _TOOL_REGISTRY.get(name)
    if func is None:
        return json.dumps({"error": f"未注册的工具：{name}"}, ensure_ascii=False)

    try:
        args = json.loads(arguments)
        result = func(**args)
        return json.dumps({"result": result}, ensure_ascii=False)
    except TypeError as e:
        return json.dumps({"error": f"参数不匹配：{e}"}, ensure_ascii=False)


# ----------------------------------------------------------------------
# 注册一个示例工具：加法计算器
# ----------------------------------------------------------------------
@tool
def add(a: float, b: float) -> float:
    """计算两个数字相加的结果"""
    return a + b


# ----------------------------------------------------------------------
# 示例工具：bash —— 让 Agent 能在本机执行 shell 命令
# ----------------------------------------------------------------------
@tool
def bash(command: str) -> str:
    """在本地执行 shell 命令并返回输出。

    会先探测本机环境，按优先级选择执行方式：
      Windows: Git Bash → 回退 cmd.exe
      其他系统: 系统默认 shell（通常 /bin/sh）
    结果会附带实际使用的执行后端（backend），供调用方确认。
    超时 30 秒，输出截断 4000 字符，防止命令挂死或撑爆上下文。
    """
    TIMEOUT = 30          # 秒
    MAX_OUTPUT = 4000     # 字符

    if os.name == "nt":
        # 1) 找 Git Bash（自包含、不依赖 WSL）
        real_bash = _find_real_bash()
        if real_bash:
            shell_cmd = [real_bash, "-lc", command]
            backend = f"git-bash ({real_bash})"
            use_shell = False
        # 2) 回退 cmd.exe（Windows 自带，永远可用）
        else:
            shell_cmd = f"cmd /c {command}"
            backend = "cmd.exe"
            use_shell = True
    else:
        # Linux / macOS：走系统默认 shell
        shell_cmd = command
        backend = "system shell"
        use_shell = True

    try:
        proc = subprocess.run(
            shell_cmd,
            shell=use_shell,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=TIMEOUT,
        )
    except subprocess.TimeoutExpired:
        return json.dumps({"error": f"命令执行超过 {TIMEOUT} 秒，已终止。", "backend": backend},
                          ensure_ascii=False)
    except OSError as e:
        return json.dumps({"error": f"命令执行失败：{e}", "backend": backend},
                          ensure_ascii=False)

    # 只保留非空的输出部分，避免把全空的字段塞给模型
    parts = []
    if proc.stdout.strip():
        parts.append(f"stdout:\n{proc.stdout.strip()}")
    if proc.stderr.strip():
        parts.append(f"stderr:\n{proc.stderr.strip()}")
    if not parts:
        parts.append("(命令执行成功，无输出)")

    text = "\n".join(parts)
    if len(text) > MAX_OUTPUT:
        text = text[:MAX_OUTPUT] + f"\n...（输出过长，已截断，剩余 {len(text) - MAX_OUTPUT} 字符）"
    return f"[backend: {backend}]\n{text}"


def _find_real_bash() -> str | None:
    """在 Windows 上定位 Git Bash 的真实 bash，找不到返回 None。

    Git Bash 的 bash 位于其安装目录，如 C:\\Program Files\\Git\\usr\\bin\\bash.exe，
    自包含、不依赖 WSL。找不到时调用方会回退到 cmd.exe。
    """
    # 1) 常见 Git Bash 安装路径
    for base in [r"C:\Program Files\Git", r"C:\Program Files (x86)\Git",
                 r"C:\Program Files\Git\mingw64"]:
        for sub in (r"usr\bin", r"bin"):
            candidate = os.path.join(base, sub, "bash.exe")
            if os.path.isfile(candidate):
                return candidate
    # 2) 搜索 PATH（排除 Windows 系统目录，避免误入 WSL 的 bash.exe 启动器）
    search_path = os.pathsep.join(
        p for p in os.environ.get("PATH", "").split(os.pathsep)
        if p and not os.path.normcase(p).startswith(os.path.normcase(r"C:\Windows"))
    )
    found = shutil.which("bash", path=search_path)
    if found:
        return found
    # 3) 从 Windows 注册表读 Git 安装位置（兼容自定义安装目录）
    try:
        import winreg
        key = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE,
                             r"SOFTWARE\GitForWindows", 0, winreg.KEY_READ)
        install_path, _ = winreg.QueryValueEx(key, "InstallPath")
        winreg.CloseKey(key)
        candidate = os.path.join(install_path, "bin", "bash.exe")
        if os.path.isfile(candidate):
            return candidate
    except OSError:
        pass
    return None


# ----------------------------------------------------------------------
# 示例工具：get_environment —— 让模型明确了解执行环境，避免误判
# ----------------------------------------------------------------------
@tool
def get_environment() -> str:
    """返回当前 Agent 执行环境的诊断信息，包括操作系统、Python、
    bash 工具使用的执行后端、以及关键命令的可执行状态。
    在调用 bash 工具之前，可以先调用本工具确认环境是否正常。
    """
    lines = [f"操作系统: {platform.platform()}"]
    lines.append(f"Python: {platform.python_version()}（{sys.executable}）")
    lines.append(f"PATH 中 python: {shutil.which('python')}")
    lines.append(f"PATH 中 python3: {shutil.which('python3')}")
    lines.append(f"PATH 中 py: {shutil.which('py')}")
    lines.append(f"bash 后端: {_find_real_bash() or '未找到 Git Bash（将回退 cmd.exe）'}")
    return "\n".join(lines)


# ----------------------------------------------------------------------
# Agent Loop 主循环
# ----------------------------------------------------------------------
def agent_loop(user_message: str, max_steps: int = 20) -> None:
    messages: list[dict] = [
        {"role": "system", "content": "你是一个有用的助手。当你需要计算时请调用工具。"},
        {"role": "user", "content": user_message},
    ]

    for step in range(1, max_steps + 1):
        print(f"\n===== 第 {step} 轮 =====")
        data = chat_completion(messages, tools=registered_tools())

        message = data["choices"][0]["message"]
        # 记录本轮消耗，便于观察
        usage = data.get("usage", {})
        print(f"[tokens] 输入={usage.get('prompt_tokens')} 输出={usage.get('completion_tokens')}")

        # 把模型的回复（可能含 tool_calls）加入历史
        messages.append(message)

        # 模型没要求调用工具 → 说明答案已经给全，结束循环
        if not message.get("tool_calls"):
            print(f"\n助手：{message.get('content')}")
            return

        # 模型要求调用工具 → 逐个执行，并以 tool 角色追加进历史
        for tool_call in message["tool_calls"]:
            tool_name = tool_call["function"]["name"]
            arguments = tool_call["function"]["arguments"]
            print(f"调用工具 <{tool_name}>，参数：{arguments}")

            result = run_tool(tool_name, arguments)

            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": tool_call["id"],  # 必须与 tool_call.id 对应
                    "content": result,
                }
            )

    print(f"\n已达到最大轮数 {max_steps}，停止循环。")


# ----------------------------------------------------------------------
if __name__ == "__main__":
    if not DEEPSEEK_API_KEY:
        print("未检测到 DEEPSEEK_API_KEY 环境变量。")
        print("1. 在 https://platform.deepseek.com 创建 API key")
        print("2. 设置环境变量后重跑，例如（PowerShell）：")
        print('      $env:DEEPSEEK_API_KEY = "你的key"')
        print("   或（CMD）：")
        print("      set DEEPSEEK_API_KEY=你的key")
        print("   可选：$env:DEEPSEEK_MODEL = \"deepseek-v4-pro\"  # 换更强推理模型")
        raise SystemExit(1)

    # 启动时打印环境诊断，便于人工确认 bash 工具会走哪个后端
    print("===== 启动环境诊断 =====")
    print(f"操作系统: {platform.platform()}")
    print(f"Python: {platform.python_version()}")
    print(f"bash 工具将使用: {_find_real_bash() or '未找到 Git Bash → 回退 cmd.exe'}")
    print("=========================")

    question = input("\n请输入你的问题（例如：计算 1234567 和 7654321 的和）：\n> ")
    agent_loop(question)
