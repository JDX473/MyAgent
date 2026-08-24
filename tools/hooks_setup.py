"""默认钩子注册：把安全校验等内置行为接入事件系统。

之后新增功能，只要再注册对应事件的回调即可，Loop 主体不再改动。
本模块被 tools/__init__.py import 以触发注册（副作用）。
"""
import json
import os
import re

from core.hooks import HookContext, HookEvents, hooks
from core.tools import _TOOL_WHITELIST


# ----------------------------------------------------------------------
# 权限校验钩子（pre_tool_execute 回调）
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


def _path_in_sysdir(path: str) -> bool:
    """路径是否落在 Windows 系统目录内。"""
    norm = os.path.normcase(os.path.normpath(os.path.abspath(path)))
    return any(norm.startswith(os.path.normcase(d)) for d in _WINDOWS_SYS_DIRS)


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


def _permission_check(ctx: HookContext) -> None | str | tuple[str, str]:
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


# ----------------------------------------------------------------------
# 示例钩子：控制台日志 / 工具结果 / token 汇总
# ----------------------------------------------------------------------
def _on_model_response(ctx: HookContext) -> None:
    last = ctx.messages[-1]
    tool_calls = last.get("tool_calls")
    if tool_calls:
        for tc in tool_calls:
            print(f"[钩子] 模型请求调用工具 {tc['function']['name']}")
    else:
        print(f"[钩子] 模型给出最终回复 {len(last.get('content') or '')} 字符")


def _on_post_tool(ctx: HookContext) -> None:
    print(f"[钩子] 工具 <{ctx.tool_name}> 执行完毕，返回 {len(ctx.result)} 字符")


def _on_stop_token_summary(ctx: HookContext) -> None:
    s = ctx.state
    total = s.get("total_prompt_tokens", 0) + s.get("total_completion_tokens", 0)
    print(f"[token 汇总] 本次会话共使用 token {total}（输入 {s.get('total_prompt_tokens', 0)} / 输出 {s.get('total_completion_tokens', 0)}）")


# ----------------------------------------------------------------------
# 注册（import 本模块即生效）
# ----------------------------------------------------------------------
hooks.register(HookEvents.PRE_TOOL_EXECUTE, _permission_check, priority=-100)
hooks.register(HookEvents.POST_MODEL_RESPONSE, _on_model_response)
hooks.register(HookEvents.POST_TOOL_EXECUTE, _on_post_tool)
hooks.register(HookEvents.STOP, _on_stop_token_summary)
