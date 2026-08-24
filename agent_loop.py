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
  设置环境变量 DEEPSEEK_API_KEY（在 https://platform.deepseek.com 申请），
  或在本目录的 .env 文件中配置（两者任一即可，已设置的环境变量优先）。
  可选 DEEPSEEK_MODEL，默认 deepseek-v4-flash。
  仅用 Python 标准库，无需 pip install 任何东西。

用法：
  python agent_loop.py
"""

import glob as glob_module
import inspect
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import urllib.error
import urllib.request
from typing import get_type_hints

# ----------------------------------------------------------------------
# 配置：原生接口，模型用 DeepSeek 当前正式命名（v4 系列）
# ----------------------------------------------------------------------
_ENV_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")


def load_env_file() -> None:
    """加载项目目录下的 .env 文件到 os.environ。

    规则：
      - .env 文件不存在则静默跳过；
      - 已设置的环境变量优先（不覆盖 os.environ 已有值）；
      - .env 中未设置的值作为兜底写入 os.environ。
      解析格式：每行 KEY=VALUE（# 开头为注释，值可带引号）。
    """
    if not os.path.isfile(_ENV_FILE):
        return
    try:
        with open(_ENV_FILE, encoding="utf-8") as f:
            for raw in f:
                line = raw.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, value = line.split("=", 1)
                key = key.strip()
                value = value.strip()
                # 去掉值两端的成对引号
                if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
                    value = value[1:-1]
                # 已存在的环境变量优先，不覆盖
                if key and key not in os.environ:
                    os.environ[key] = value
    except OSError:
        pass


load_env_file()

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
# 工具注册表 + 执行白名单：@tool 装饰器 + 自动生成 schema，扩展只需写普通函数
# ----------------------------------------------------------------------
# 注册规则（由装饰器自动完成，无需手动填 JSON）：
#   函数名        -> 工具名
#   函数 docstring -> 工具描述（可选）
#   参数的类型注解 -> parameters schema（必填）
# 安全约束：@tool 注册时会同步加入"执行白名单"；不在白名单内的工具
#   即使被模型点名也不会执行（run_tool 与权限层双重拦截）。
_TOOL_REGISTRY: dict[str, callable] = {}
_TOOL_WHITELIST: set[str] = set()


def tool(func=None, *, name: str | None = None):
    """把被装饰的函数注册为 Agent 可用工具。

    支持两种写法：@tool  或  @tool()  或  @tool(name="别名")
    函数需写参数类型注解（int/float/str/bool），并尽量写一句 docstring
    作为工具描述。注册即入执行白名单。
    """

    def decorator(f):
        fname = name or f.__name__
        f._tool_name = fname  # 标记，供自动生成 schema 时识别
        _TOOL_REGISTRY[fname] = f
        _TOOL_WHITELIST.add(fname)   # 同步加入执行白名单
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

    执行前校验执行白名单：不在白名单内的工具一律不执行。
    以后加新工具：只要用 @tool 注册即可，这里无需再改。
    """
    # 白名单校验：注册即入白名单，未入白名单的不执行
    if name not in _TOOL_WHITELIST:
        return json.dumps(
            {"blocked": f"工具 {name} 不在执行白名单内，已拒绝执行。"}, ensure_ascii=False)

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
# Hook 事件系统：把 Loop 的生命周期抽象成可插拔的钩子
# ----------------------------------------------------------------------
# 固定的 5 个事件点（Hooks 枚举），Loop 主体不再改动：
#   USER_PROMPT_SUBMIT  用户输入提交后、首轮请求前
#   POST_MODEL_RESPONSE 模型每轮响应后（无论是否调用工具）
#   PRE_TOOL_EXECUTE    工具执行前（可拦截/询问，见 HookContext.verdict）
#   POST_TOOL_EXECUTE   工具执行后（拿到工具返回结果）
#   STOP                循环结束时（无论正常返回还是达到上限）
#
# 用法：
#   hooks.register(Hooks.PRE_TOOL_EXECUTE, callback, priority=0)
#   callback(ctx: HookContext) -> None | "allow" | "confirm" | ("deny", 原因)
#   - 返回 None：视为 allow（继续）
#   - 返回 "allow"：继续
#   - 返回 "confirm"：标记需要用户确认
#   - 返回 ("deny", 原因)：拒绝执行，原因会回给模型
#   同一事件多个回调按 priority 升序执行；pre 的裁决取最高优先级
#   （deny > confirm > allow）。
class HookContext:
    """一次工具调用 / 一个生命周期事件内，钩子间共享的上下文。"""

    def __init__(self) -> None:
        self.messages: list[dict] = []   # 当前对话的完整消息历史
        self.tool_name: str = ""         # 本次要调用的工具名（pre/post tool）
        self.arguments: str = ""         # 模型传来的参数 JSON 字符串（pre/post tool）
        self.result: str = ""            # 工具执行后的 JSON 结果字符串（post tool）
        self.verdict: str | None = None  # pre 钩子的裁决：allow / confirm / deny
        self.deny_reason: str = ""       # deny 时的原因说明
        self.state: dict = {}            # 任意共享状态（跨钩子、跨轮次）

    def deny(self, reason: str) -> tuple[str, str]:
        """便捷方法：返回 ("deny", reason)。"""
        return ("deny", reason)


# 钩子事件名
HOOK_USER_PROMPT_SUBMIT = "user_prompt_submit"
HOOK_POST_MODEL_RESPONSE = "post_model_response"
HOOK_PRE_TOOL_EXECUTE = "pre_tool_execute"
HOOK_POST_TOOL_EXECUTE = "post_tool_execute"
HOOK_STOP = "stop"


class HookRegistry:
    """按事件名注册/触发钩子回调。"""

    def __init__(self) -> None:
        self._handlers: dict[str, list[tuple[int, callable]]] = {
            HOOK_USER_PROMPT_SUBMIT: [],
            HOOK_POST_MODEL_RESPONSE: [],
            HOOK_PRE_TOOL_EXECUTE: [],
            HOOK_POST_TOOL_EXECUTE: [],
            HOOK_STOP: [],
        }

    def register(self, event: str, callback: callable, priority: int = 0) -> None:
        """注册一个钩子。priority 越小越先执行（同事件内）。"""
        if event not in self._handlers:
            raise ValueError(f"未知事件：{event}")
        self._handlers[event].append((priority, callback))
        self._handlers[event].sort(key=lambda p: p[0])

    def _run(self, event: str, ctx: HookContext) -> None:
        """顺序执行某事件的全部回调。"""
        for _, cb in self._handlers[event]:
            print(f"【{event}：{cb.__name__}】")
            cb(ctx)

    def fire(self, event: str, ctx: HookContext) -> None:
        """触发一个非工具事件（user_prompt_submit / post_model_response / stop）。"""
        self._run(event, ctx)

    def fire_pre_tool(self, ctx: HookContext) -> None:
        """触发 pre_tool_execute，聚合所有回调的裁决（deny > confirm > allow）。"""
        ctx.verdict = None
        for _, cb in self._handlers[HOOK_PRE_TOOL_EXECUTE]:
            print(f"【{HOOK_PRE_TOOL_EXECUTE}：{cb.__name__}】")
            result = cb(ctx)
            if result is None:
                continue
            if result == "allow":
                if ctx.verdict is None:
                    ctx.verdict = "allow"
            elif result == "confirm":
                ctx.verdict = "confirm"
            elif isinstance(result, tuple) and result[0] == "deny":
                ctx.verdict = "deny"
                ctx.deny_reason = result[1]
                break  # deny 最高优先级，直接短路
        ctx.verdict = ctx.verdict or "allow"

    def fire_post_tool(self, ctx: HookContext) -> None:
        """触发 post_tool_execute。"""
        self._run(HOOK_POST_TOOL_EXECUTE, ctx)


hooks = HookRegistry()


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
# 文件工具：read / write / edit / glob —— 纯 Python 实现，不经过 shell
# ----------------------------------------------------------------------
# 说明：比走 bash 更可靠（无路径转义问题），也更安全可控。
# 统一安全限制：读写只允许在 Agent 工作目录内进行，防止模型越权操作
# 系统其它位置。
_MAX_READ_CHARS = 20000   # read 单次最多返回的字符数
_MAX_LIST_ENTRIES = 100   # glob 单次最多返回的条目数
_WORK_DIR = os.path.abspath(os.getcwd())  # Agent 工作目录 = 启动脚本时所在的目录


def _safe_path(path: str) -> str:
    """把用户给出的路径解析为绝对路径，并校验在工作目录内。

    越界（.. 跳出、绝对路径指向其它盘、系统目录等）时抛出 ValueError。
    """
    if not isinstance(path, str) or not path.strip():
        raise ValueError("路径不能为空")
    norm = os.path.normpath(path)
    # 非绝对路径一律以工作目录为基准拼接
    if not os.path.isabs(norm):
        norm = os.path.abspath(os.path.join(_WORK_DIR, norm))
    else:
        norm = os.path.abspath(norm)
    work = os.path.normcase(_WORK_DIR)
    real = os.path.normcase(norm)
    # 必须在工作目录内，且不允许精确等于工作目录本身（read/write/edit 都需要文件）
    if not (real == work or real.startswith(work + os.sep)):
        raise ValueError(f"路径超出工作目录（{_WORK_DIR}），已拒绝：{path}")
    return norm


@tool
def read(path: str) -> str:
    """阅读一个文本文件的内容，返回文件文本。

    只允许读取 Agent 工作目录内的文件，路径可用相对路径或绝对路径。
    最多返回前 20000 字符。
    """
    try:
        full = _safe_path(path)
        if not os.path.isfile(full):
            return json.dumps({"error": f"文件不存在：{path}"}, ensure_ascii=False)
        with open(full, encoding="utf-8", errors="replace") as f:
            text = f.read(_MAX_READ_CHARS + 1)
        if len(text) > _MAX_READ_CHARS:
            text = text[:_MAX_READ_CHARS] + f"\n...（文件较大，已截断，仅显示前 {_MAX_READ_CHARS} 字符）"
        return json.dumps({"content": text}, ensure_ascii=False)
    except ValueError as e:
        return json.dumps({"error": str(e)}, ensure_ascii=False)
    except OSError as e:
        return json.dumps({"error": f"读取失败：{e}"}, ensure_ascii=False)


@tool
def write(path: str, content: str) -> str:
    """向指定文件写入内容。

    若文件已存在则覆盖其内容；若文件不存在则创建（自动创建父目录）。
    只允许操作 Agent 工作目录内的路径。
    """
    try:
        full = _safe_path(path)
        parent = os.path.dirname(full)
        os.makedirs(parent, exist_ok=True)
        with open(full, "w", encoding="utf-8") as f:
            f.write(content)
        return json.dumps({"ok": True, "path": full, "chars": len(content)}, ensure_ascii=False)
    except ValueError as e:
        return json.dumps({"error": str(e)}, ensure_ascii=False)
    except OSError as e:
        return json.dumps({"error": f"写入失败：{e}"}, ensure_ascii=False)


@tool
def edit(path: str, old: str, new: str) -> str:
    """修改文件内容：把文件中的 old 替换为 new。

    若 old 在文件中出现多次，只替换第一处；old 未找到则返回错误。
    只允许操作 Agent 工作目录内的文件。
    """
    try:
        full = _safe_path(path)
        if not os.path.isfile(full):
            return json.dumps({"error": f"文件不存在：{path}"}, ensure_ascii=False)
        with open(full, encoding="utf-8", errors="replace") as f:
            text = f.read()
        if old not in text:
            return json.dumps(
                {"error": f"未在文件中找到要替换的内容：{old!r}"}, ensure_ascii=False)
        text = text.replace(old, new, 1)  # 只替换第一处
        with open(full, "w", encoding="utf-8") as f:
            f.write(text)
        return json.dumps({"ok": True, "path": full}, ensure_ascii=False)
    except ValueError as e:
        return json.dumps({"error": str(e)}, ensure_ascii=False)
    except OSError as e:
        return json.dumps({"error": f"修改失败：{e}"}, ensure_ascii=False)


@tool
def glob(pattern: str) -> str:
    """按通配符模式查找工作目录内的文件/目录，返回匹配的路径列表。

    pattern 支持常见的 glob 通配符：* 匹配任意字符（不含路径分隔符）、
    ** 递归匹配子目录、? 匹配单个字符、[abc] 匹配字符集合。
    相对路径以工作目录为基准；最多返回前 100 条。
    """
    try:
        if not pattern.strip():
            return json.dumps({"error": "pattern 不能为空"}, ensure_ascii=False)
        # 先校验基路径在工作目录内，杜绝 ../ 越界
        base = _safe_path(os.path.join(_WORK_DIR, pattern.lstrip("/\\")))
        matches = sorted(glob_module.glob(base, recursive=True))
        # 过滤越界结果（正常情况下不会出现，双保险）
        safe_matches = []
        for m in matches:
            try:
                _safe_path(m)
                safe_matches.append(m)
            except ValueError:
                pass
        total = len(safe_matches)
        if total > _MAX_LIST_ENTRIES:
            shown = safe_matches[:_MAX_LIST_ENTRIES]
            note = f"（共 {total} 条，仅显示前 {_MAX_LIST_ENTRIES} 条）"
        else:
            shown, note = safe_matches, ""
        return json.dumps({"matches": shown, "total": total, "note": note}, ensure_ascii=False)
    except ValueError as e:
        return json.dumps({"error": str(e)}, ensure_ascii=False)
    except OSError as e:
        return json.dumps({"error": f"查找失败：{e}"}, ensure_ascii=False)


# ----------------------------------------------------------------------
# 权限校验层：所有工具调用在执行前都会经过这里
# ----------------------------------------------------------------------
# 决策模型（三级）：
#   1. 危险操作  -> deny     直接拒绝，不询问（删根/格式化/关机/写系统目录等）
#   2. 不确定    -> confirm  阻塞等待用户输入，按用户裁决放行或拒绝
#   3. 无危害    -> allow    直接执行
#
# 判据来源：
#   - 参数值里的危险命令关键字（bash 的 command、文件的 path）
#   - 工具本身的风险等级（edit 必然覆盖、bash 不受控）
#   - 写入目标是否已存在（write 覆盖已有文件属于破坏性操作）
_VERDICT_DENY = "deny"
_VERDICT_CONFIRM = "confirm"
_VERDICT_ALLOW = "allow"

# 危险 bash 命令：正则命中即直接拒绝
_BASH_DENY_RE = re.compile(
    r"\b(?:rm\s+-(?:rf|fr|r\s+f|f\s+r|r\s*[a-zA-Z])\s*[/.]?\s*$|"
    r"rm\s+-(?:rf|fr|r)\s+[/]|rm\s+-rf\s+/?[a-zA-Z]:|rm\s+-rf\s+/\s*$|"
    r"rm\s+-rf\s+/\s|sudo\s+rm\s+-rf\s+/\s*$|"
    r"del\s+/[a-z]\s+[a-zA-Z]:|rmdir\s+/s\s+[a-zA-Z]:|"
    r"format\s+[a-zA-Z]:|diskpart|fdisk|mkfs|shutdown|reboot|restart|"
    r"reg\s+(?:delete|add)\s+HK|wget|curl|nc\s|telnet|nmap|hydra|dd\s+if=)",
    re.IGNORECASE,
)

# 需要用户确认的 bash 命令（比 deny 低一档：有破坏性但不是致命）
_BASH_CONFIRM_RE = re.compile(
    r"\b(?:rm|mv|cp|del|mkdir|rmdir|git\s+push|git\s+reset|git\s+clean|"
    r"git\s+rebase|git\s+fetch|git\s+pull|git\s+checkout\s+\.|"
    r"pip\s+(?:install|uninstall)|npm\s+(?:install|uninstall)|"
    r"apt-get|yum|dnf|brew\s+install)\b",
    re.IGNORECASE,
)

# 明显无害的 bash 命令，直接放行（其余一律走 confirm，保持保守）
_BASH_ALLOW_RE = re.compile(
    r"^(?:ls|ls\s|pwd|echo\s|cat\s|head\s|tail\s|grep\s|find\s|"
    r"which\s|whoami|python\s+--version|python3\s+--version|date|uname|"
    r"git\s+status|git\s+log|git\s+diff\s|git\s+show\s|cd\s|"
    r"sort\s|wc\s|uniq\s|env\b|set\b|hostname|wc\b)",
    re.IGNORECASE,
)

# 写在 Windows 系统目录里的路径：直接拒绝（即使走的是 read/write/edit）
_WINDOWS_SYS_DIRS = (r"C:\Windows", r"C:\Program Files", r"C:\Program Files (x86)")


def _basename_lower(path: str) -> str:
    """提取路径末尾的文件名并小写，便于按名识别命令。"""
    return os.path.basename(path.rstrip("/\\")).lower()


def _path_in_sysdir(path: str) -> bool:
    """路径是否落在 Windows 系统目录内。"""
    norm = os.path.normcase(os.path.normpath(os.path.abspath(path)))
    return any(norm.startswith(os.path.normcase(d)) for d in _WINDOWS_SYS_DIRS)


def _permission_check(ctx) -> None | str | tuple[str, str]:
    """权限校验钩子（pre_tool_execute 回调）。

    决策模型（三级）：
      1. 危险操作  -> deny     直接拒绝，不询问（删根/格式化/关机/写系统目录等）
      2. 不确定    -> confirm  阻塞等待用户输入，按用户裁决放行或拒绝
      3. 无危害    -> allow    直接执行

    返回 None 表示放行（无拦截）；返回 "confirm" / ("deny", 原因) 则拦截。
    """
    tool_name = ctx.tool_name
    arguments = ctx.arguments

    # ---- 第 0 级：不在执行白名单内 → 直接拒绝（不询问）----
    if tool_name not in _TOOL_WHITELIST:
        return ctx.deny(f"工具 {tool_name} 不在执行白名单内，已拒绝执行。")

    # ---- 第一级：危险操作，直接拒绝 ----
    if tool_name == "bash":
        cmd = _bash_command(arguments)
        if cmd is None:
            return None  # 解析不到 command，交给 run_tool 的正常参数校验
        if _BASH_DENY_RE.search(cmd):
            return ctx.deny(f"命令命中危险规则，已拒绝：{cmd}")
        if _path_in_sysdir(cmd) or _has_dangerous_path(cmd):
            return ctx.deny(f"命令涉及系统关键位置，已拒绝：{cmd}")

    elif tool_name in ("read", "write", "edit", "glob"):
        path = _file_path(arguments)
        if path is not None and _path_in_sysdir(path):
            return ctx.deny(f"路径位于系统目录，已拒绝：{path}")

    # ---- 第二级：不确定/有破坏性，询问用户 ----
    if tool_name == "bash":
        if not _BASH_ALLOW_RE.match(cmd):
            return "confirm"  # 不在白名单里的 bash 命令都要问
        return None
    if tool_name == "write":
        if _file_exists(arguments):
            return "confirm"  # 覆盖已有文件 -> 问
        return None           # 新建文件 -> 放行
    if tool_name == "edit":
        return "confirm"      # 修改文件必然有破坏性 -> 问
    if tool_name == "read":
        return None           # 读文件无害
    if tool_name == "glob":
        return None           # 查找无害

    # 其它工具（如 get_environment）：无害，直接放行
    return None


def _bash_command(arguments: str) -> str | None:
    """从参数 JSON 里提取 bash 的 command 字段。"""
    try:
        return json.loads(arguments).get("command")
    except (json.JSONDecodeError, AttributeError):
        return None


def _file_path(arguments: str) -> str | None:
    """从参数 JSON 里提取文件类工具的 path 字段。"""
    try:
        return json.loads(arguments).get("path")
    except (json.JSONDecodeError, AttributeError):
        return None


def _file_exists(arguments: str) -> bool:
    """write 的目标文件是否已存在（决定要不要询问）。"""
    path = _file_path(arguments)
    if not path:
        return False
    try:
        return os.path.exists(os.path.abspath(path))
    except (ValueError, OSError):
        return False


def _has_dangerous_path(cmd: str) -> bool:
    """命令里是否包含对系统关键位置的写入/删除意图。"""
    lowered = cmd.lower()
    return any(d in lowered for d in (
        r"c:\windows", r"c:\program files", "/etc/passwd", "/etc/shadow",
        "~/.ssh", r"\\.\\", "rd /s", "format",
    ))


def _prompt_user(ctx: HookContext) -> bool:
    """阻塞等待用户输入，决定是否放行一次"需确认"的工具调用。"""
    cmd = _bash_command(ctx.arguments) if ctx.tool_name == "bash" else _file_path(ctx.arguments)
    print(f"\n[权限] 工具 <{ctx.tool_name}> 需要你确认：{cmd}")
    while True:
        answer = input("      允许执行？(y=允许 / n=拒绝 / 其它=拒绝)：").strip().lower()
        if answer in ("y", "yes"):
            return True
        if answer in ("n", "no", ""):
            return False
        print("      请输入 y 或 n。")


# ----------------------------------------------------------------------
# 默认钩子注册：把安全校验等内置行为接入事件系统
# ----------------------------------------------------------------------
# 之后新增功能，只要再注册对应事件的回调即可，Loop 主体不再改动。
hooks.register(HOOK_PRE_TOOL_EXECUTE, _permission_check, priority=-100)

# 示例钩子①：记录"调用工具"到控制台（post_model_response 触发，含工具调用）
def _on_model_response(ctx: HookContext) -> None:
    last = ctx.messages[-1]
    tool_calls = last.get("tool_calls")
    if tool_calls:
        for tc in tool_calls:
            print(f"[钩子] 模型请求调用工具 {tc['function']['name']}")
    else:
        print(f"[钩子] 模型给出最终回复 {len(last.get('content') or '')} 字符")


hooks.register(HOOK_POST_MODEL_RESPONSE, _on_model_response)

# 示例钩子②：每次工具执行后打印耗时（post_tool_execute 触发）
def _on_post_tool(ctx: HookContext) -> None:
    print(f"[钩子] 工具 <{ctx.tool_name}> 执行完毕，返回 {len(ctx.result)} 字符")


hooks.register(HOOK_POST_TOOL_EXECUTE, _on_post_tool)

# 示例钩子③：循环结束（STOP）时，输出本次会话累计使用的 token 数量
def _on_stop_token_summary(ctx: HookContext) -> None:
    s = ctx.state
    total = s.get("total_prompt_tokens", 0) + s.get("total_completion_tokens", 0)
    print(f"[token 汇总] 本次会话共使用 token {total}（输入 {s.get('total_prompt_tokens', 0)} / 输出 {s.get('total_completion_tokens', 0)}）")


hooks.register(HOOK_STOP, _on_stop_token_summary)


# ----------------------------------------------------------------------
# Agent Loop 主循环：逻辑固定，扩展通过钩子完成
# ----------------------------------------------------------------------
def agent_loop(user_message: str, max_steps: int = 20) -> None:
    messages: list[dict] = [
        {"role": "system", "content": "你是一个有用的助手。当需要访问本地环境或执行命令时，请调用可用的工具。"},
        {"role": "user", "content": user_message},
    ]
    ctx = HookContext()
    ctx.messages = messages

    # 用户输入提交后、首轮请求前
    hooks.fire(HOOK_USER_PROMPT_SUBMIT, ctx)

    for step in range(1, max_steps + 1):
        print(f"\n===== 第 {step} 轮 =====")
        data = chat_completion(messages, tools=registered_tools())

        message = data["choices"][0]["message"]
        # 记录本轮消耗，便于观察，并累加到共享状态
        usage = data.get("usage", {})
        print(f"[tokens] 输入={usage.get('prompt_tokens')} 输出={usage.get('completion_tokens')}")
        s = ctx.state
        s["total_prompt_tokens"] = s.get("total_prompt_tokens", 0) + (usage.get("prompt_tokens") or 0)
        s["total_completion_tokens"] = s.get("total_completion_tokens", 0) + (usage.get("completion_tokens") or 0)

        # 把模型的回复（可能含 tool_calls）加入历史
        messages.append(message)

        # 模型每轮响应后（无论是否调用工具）
        hooks.fire(HOOK_POST_MODEL_RESPONSE, ctx)

        # 模型没要求调用工具 → 说明答案已经给全，结束循环
        if not message.get("tool_calls"):
            print(f"\n助手：{message.get('content')}")
            hooks.fire(HOOK_STOP, ctx)
            return

        # 模型要求调用工具 → 逐个执行，并以 tool 角色追加进历史
        for tool_call in message["tool_calls"]:
            tool_name = tool_call["function"]["name"]
            arguments = tool_call["function"]["arguments"]
            print(f"调用工具 <{tool_name}>，参数：{arguments}")

            ctx.tool_name = tool_name
            ctx.arguments = arguments

            # 工具执行前：钩子裁决（deny > confirm > allow）
            hooks.fire_pre_tool(ctx)
            verdict = ctx.verdict or "allow"

            if verdict == "confirm":
                if _prompt_user(ctx):
                    result = run_tool(tool_name, arguments)
                else:
                    result = json.dumps(
                        {"blocked": "用户拒绝了该次操作，请勿重试。"}, ensure_ascii=False)
            elif verdict == "deny":
                reason = ctx.deny_reason or "该调用被安全策略拒绝"
                result = json.dumps({"blocked": reason}, ensure_ascii=False)
            else:  # allow
                result = run_tool(tool_name, arguments)

            ctx.result = result
            # 工具执行后
            hooks.fire_post_tool(ctx)

            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": tool_call["id"],  # 必须与 tool_call.id 对应
                    "content": result,
                }
            )

    print(f"\n已达到最大轮数 {max_steps}，停止循环。")
    hooks.fire(HOOK_STOP, ctx)


# ----------------------------------------------------------------------
if __name__ == "__main__":
    if not DEEPSEEK_API_KEY:
        print("未检测到 DEEPSEEK_API_KEY。可通过以下任一方式配置：")
        print("1. 设置环境变量，例如（PowerShell）：")
        print('      $env:DEEPSEEK_API_KEY = "你的key"')
        print("2. 在本项目目录创建 .env 文件（推荐，已被 git 忽略）：")
        print('      DEEPSEEK_API_KEY=你的key')
        print("   可选在 .env 中配置：DEEPSEEK_MODEL=deepseek-v4-pro")
        raise SystemExit(1)

    # # 启动时打印环境诊断，便于人工确认 bash 工具会走哪个后端
    # print("===== 启动环境诊断 =====")
    # print(f"操作系统: {platform.platform()}")
    # print(f"Python: {platform.python_version()}")
    # print(f"bash 工具将使用: {_find_real_bash() or '未找到 Git Bash → 回退 cmd.exe'}")
    # print("=========================")

    question = input("\n请输入你的问题：\n> ")
    agent_loop(question)
