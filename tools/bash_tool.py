"""bash 工具：让 Agent 能在本机执行 shell 命令。"""
import json
import os
import shutil
import subprocess

from core.tools import tool


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
