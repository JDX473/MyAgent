"""get_environment 工具：让模型明确了解执行环境，避免误判。"""
import os
import platform
import shutil
import sys

from core.tools import tool

from tools.bash_tool import _find_real_bash


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
