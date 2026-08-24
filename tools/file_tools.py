"""文件工具：read / write / edit / glob —— 纯 Python 实现，不经过 shell。

说明：比走 bash 更可靠（无路径转义问题），也更安全可控。
统一安全限制：读写只允许在 Agent 工作目录内进行，防止模型越权操作
系统其它位置。
"""
import glob as glob_module
import json
import os

from core.tools import tool

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
