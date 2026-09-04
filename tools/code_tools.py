"""代码检查工具：搜索源码、查找 Python 符号、按行阅读代码。

这些工具都是只读操作，统一复用文件工具的工作目录沙箱。
"""
import ast
import json
import os
import re
import shutil
import subprocess

import tools.file_tools as file_tools
from core.tools import tool

_DEFAULT_LIMIT = 20
_MAX_LIMIT = 100
_MAX_CONTEXT = 5
_MAX_READ_LINES = 200
_RG_TIMEOUT = 15

_EXCLUDED_DIRS = {
    ".git",
    ".agents",
    ".codex",
    ".cache",
    ".mypy_cache",
    ".nox",
    ".pytest_cache",
    ".ruff_cache",
    ".tox",
    ".venv",
    "__pycache__",
    "build",
    "coverage",
    "dist",
    "env",
    "generated",
    "htmlcov",
    "node_modules",
    "target",
    "venv",
}
_EXCLUDED_DIR_SUFFIXES = (".egg-info",)
_EXCLUDED_FILE_SUFFIXES = (
    ".class",
    ".dll",
    ".exe",
    ".jar",
    ".min.css",
    ".min.js",
    ".o",
    ".obj",
    ".pdf",
    ".png",
    ".pyc",
    ".pyd",
    ".pyo",
    ".so",
    ".zip",
)

_TEXT_EXTENSIONS = {
    ".bat",
    ".c",
    ".cfg",
    ".conf",
    ".cpp",
    ".cs",
    ".css",
    ".csv",
    ".go",
    ".gradle",
    ".h",
    ".hpp",
    ".html",
    ".ini",
    ".java",
    ".js",
    ".json",
    ".jsx",
    ".kt",
    ".kts",
    ".log",
    ".md",
    ".php",
    ".properties",
    ".proto",
    ".ps1",
    ".py",
    ".pyw",
    ".rb",
    ".rs",
    ".rst",
    ".scala",
    ".scss",
    ".sh",
    ".sql",
    ".swift",
    ".toml",
    ".ts",
    ".tsx",
    ".txt",
    ".xml",
    ".yaml",
    ".yml",
}
_TEXT_FILENAMES = {
    ".dockerignore",
    ".editorconfig",
    ".gitignore",
    "dockerfile",
    "makefile",
    "pyproject.toml",
}
_TEXT_PREFIXES = ("readme", "license", "changelog", "requirements")

_RG_INCLUDE_GLOBS = [
    "*.bat",
    "*.c",
    "*.cfg",
    "*.conf",
    "*.cpp",
    "*.cs",
    "*.css",
    "*.csv",
    "*.go",
    "*.gradle",
    "*.h",
    "*.hpp",
    "*.html",
    "*.ini",
    "*.java",
    "*.js",
    "*.json",
    "*.jsx",
    "*.kt",
    "*.kts",
    "*.log",
    "*.md",
    "*.php",
    "*.properties",
    "*.proto",
    "*.ps1",
    "*.py",
    "*.pyw",
    "*.rb",
    "*.rs",
    "*.rst",
    "*.scala",
    "*.scss",
    "*.sh",
    "*.sql",
    "*.swift",
    "*.toml",
    "*.ts",
    "*.tsx",
    "*.txt",
    "*.xml",
    "*.yaml",
    "*.yml",
    "README*",
    "LICENSE*",
    "CHANGELOG*",
    "Dockerfile",
    "Makefile",
    ".dockerignore",
    ".editorconfig",
    ".gitignore",
    "requirements*",
]
_RG_EXCLUDE_GLOBS = [f"!**/{d}/**" for d in sorted(_EXCLUDED_DIRS)]
_RG_EXCLUDE_GLOBS.extend([f"!**/*{suffix}/**" for suffix in _EXCLUDED_DIR_SUFFIXES])
_RG_EXCLUDE_GLOBS.extend([f"!**/*{suffix}" for suffix in _EXCLUDED_FILE_SUFFIXES])


def _work_dir() -> str:
    return os.path.abspath(file_tools._WORK_DIR)


def _safe_target(path: str) -> str:
    if not isinstance(path, str) or not path.strip():
        return _work_dir()
    return file_tools._safe_path(path)


def _clean_relpath(full_path: str) -> str:
    rel = os.path.relpath(os.path.abspath(full_path), _work_dir())
    if rel == ".":
        return "."
    return rel.replace("\\", "/")


def _norm_sort_key(full_path: str) -> str:
    return os.path.normcase(_clean_relpath(full_path))


def _is_excluded_rel(rel_path: str) -> bool:
    if rel_path == ".":
        return False
    parts = rel_path.replace("\\", "/").split("/")
    for part in parts[:-1]:
        lower = part.lower()
        if lower in _EXCLUDED_DIRS or lower.endswith(_EXCLUDED_DIR_SUFFIXES):
            return True
    name = parts[-1].lower()
    return name.endswith(_EXCLUDED_FILE_SUFFIXES)


def _is_text_candidate(full_path: str) -> bool:
    if _is_excluded_rel(_clean_relpath(full_path)):
        return False
    name = os.path.basename(full_path)
    lower_name = name.lower()
    if lower_name in _TEXT_FILENAMES:
        return True
    if lower_name.startswith(_TEXT_PREFIXES):
        return True
    return os.path.splitext(lower_name)[1] in _TEXT_EXTENSIONS


def _looks_binary(full_path: str) -> bool:
    try:
        with open(full_path, "rb") as f:
            return b"\0" in f.read(4096)
    except OSError:
        return True


def _is_searchable_file(full_path: str) -> bool:
    return os.path.isfile(full_path) and _is_text_candidate(full_path) and not _looks_binary(full_path)


def _iter_searchable_files(target: str):
    if os.path.isfile(target):
        if _is_searchable_file(target):
            yield target
        return

    for root, dirs, files in os.walk(target):
        dirs[:] = [
            d for d in sorted(dirs)
            if not _is_excluded_rel(_clean_relpath(os.path.join(root, d, "__placeholder__")))
        ]
        for filename in sorted(files):
            full_path = os.path.join(root, filename)
            if _is_searchable_file(full_path):
                yield full_path


def _iter_python_files(target: str):
    for full_path in _iter_searchable_files(target):
        if os.path.splitext(full_path.lower())[1] in (".py", ".pyw"):
            yield full_path


def _bounded_int(value, default: int, minimum: int, maximum: int, field: str) -> tuple[int | None, str | None]:
    if value is None:
        return default, None
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None, f"{field} 必须是整数"
    return max(minimum, min(parsed, maximum)), None


def _read_file_lines(full_path: str) -> list[str]:
    with open(full_path, encoding="utf-8", errors="replace") as f:
        return f.readlines()


def _strip_line(raw: str) -> str:
    return raw.rstrip("\r\n")


def _line_window(full_path: str, line_number: int, context: int, line_cache: dict[str, list[str]]) -> tuple[str, list[dict]]:
    lines = line_cache.get(full_path)
    if lines is None:
        lines = _read_file_lines(full_path)
        line_cache[full_path] = lines
    if line_number < 1 or line_number > len(lines):
        return "", []
    start = max(1, line_number - context)
    end = min(len(lines), line_number + context)
    return _strip_line(lines[line_number - 1]), [
        {"line": idx, "text": _strip_line(lines[idx - 1])}
        for idx in range(start, end + 1)
    ]


def _target_arg(target: str) -> str:
    rel = os.path.relpath(target, _work_dir())
    return "." if rel == "." else rel


def _full_from_rg_path(path_text: str) -> str:
    if os.path.isabs(path_text):
        return os.path.abspath(path_text)
    return os.path.abspath(os.path.join(_work_dir(), path_text))


def _search_with_rg(query: str, target: str, regex: bool, case_sensitive: bool) -> tuple[list[tuple[str, int]], str | None]:
    if shutil.which("rg") is None:
        return [], "missing"

    args = [
        "rg",
        "--json",
        "--line-number",
        "--color",
        "never",
        "--no-ignore",
    ]
    if not regex:
        args.append("--fixed-strings")
    if not case_sensitive:
        args.append("--ignore-case")
    for glob in _RG_INCLUDE_GLOBS:
        args.extend(["--glob", glob])
    for glob in _RG_EXCLUDE_GLOBS:
        args.extend(["--glob", glob])
    args.extend(["--", query, _target_arg(target)])

    try:
        proc = subprocess.run(
            args,
            cwd=_work_dir(),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=_RG_TIMEOUT,
        )
    except FileNotFoundError:
        return [], "missing"
    except subprocess.TimeoutExpired:
        return [], f"rg 搜索超过 {_RG_TIMEOUT} 秒，已终止"
    except OSError as e:
        return [], f"rg 搜索失败：{e}"

    if proc.returncode not in (0, 1):
        detail = (proc.stderr or proc.stdout or "").strip()
        return [], f"rg 搜索失败：{detail or f'退出码 {proc.returncode}'}"

    matches: list[tuple[str, int]] = []
    seen = set()
    for raw in proc.stdout.splitlines():
        try:
            record = json.loads(raw)
        except ValueError:
            continue
        if record.get("type") != "match":
            continue
        data = record.get("data") or {}
        line_number = data.get("line_number")
        path_info = data.get("path") or {}
        path_text = path_info.get("text") or path_info.get("bytes")
        if isinstance(path_text, bytes):
            path_text = path_text.decode("utf-8", errors="replace")
        if not isinstance(path_text, str) or not isinstance(line_number, int):
            continue
        full_path = _full_from_rg_path(path_text)
        try:
            file_tools._safe_path(full_path)
        except ValueError:
            continue
        if not _is_searchable_file(full_path):
            continue
        key = (os.path.normcase(full_path), line_number)
        if key not in seen:
            seen.add(key)
            matches.append((full_path, line_number))
    return matches, None


def _compile_matcher(query: str, regex: bool, case_sensitive: bool):
    if regex:
        flags = 0 if case_sensitive else re.IGNORECASE
        pattern = re.compile(query, flags)
        return lambda line: pattern.search(line) is not None
    if case_sensitive:
        return lambda line: query in line
    lowered = query.lower()
    return lambda line: lowered in line.lower()


def _search_with_python(query: str, target: str, regex: bool, case_sensitive: bool) -> tuple[list[tuple[str, int]], str | None]:
    try:
        matcher = _compile_matcher(query, regex, case_sensitive)
    except re.error as e:
        return [], f"regex 无效：{e}"

    matches: list[tuple[str, int]] = []
    for full_path in _iter_searchable_files(target):
        try:
            with open(full_path, encoding="utf-8", errors="replace") as f:
                for idx, line in enumerate(f, start=1):
                    if matcher(line):
                        matches.append((full_path, idx))
        except OSError:
            continue
    return matches, None


def _format_code_hits(
    matches: list[tuple[str, int]],
    limit: int,
    context: int,
) -> list[dict]:
    line_cache: dict[str, list[str]] = {}
    hits = []
    for full_path, line_number in matches[:limit]:
        snippet, surrounding = _line_window(full_path, line_number, context, line_cache)
        hits.append({
            "file": _clean_relpath(full_path),
            "line": line_number,
            "snippet": snippet,
            "context": surrounding,
        })
    return hits


@tool
def search_code(
    query: str,
    path: str = "",
    regex: bool = False,
    case_sensitive: bool = False,
    limit: int = _DEFAULT_LIMIT,
    context: int = 2,
) -> str:
    """在工作目录内搜索源码/配置/文档文本，返回文件、行号、匹配行与上下文。

    path 可限制到某个子目录或文件；默认搜索整个工作目录。优先使用 rg，
    找不到 rg 时自动回退到纯 Python 扫描。会跳过缓存、生成目录和二进制文件。
    """
    if not isinstance(query, str) or not query:
        return json.dumps({"error": "query 不能为空"}, ensure_ascii=False)
    limit_int, error = _bounded_int(limit, _DEFAULT_LIMIT, 1, _MAX_LIMIT, "limit")
    if error:
        return json.dumps({"error": error}, ensure_ascii=False)
    context_int, error = _bounded_int(context, 2, 0, _MAX_CONTEXT, "context")
    if error:
        return json.dumps({"error": error}, ensure_ascii=False)

    try:
        target = _safe_target(path)
    except ValueError as e:
        return json.dumps({"error": str(e)}, ensure_ascii=False)
    if not os.path.exists(target):
        return json.dumps({"error": f"路径不存在：{path or '.'}"}, ensure_ascii=False)
    if not (os.path.isfile(target) or os.path.isdir(target)):
        return json.dumps({"error": f"路径不是文件或目录：{path or '.'}"}, ensure_ascii=False)

    matches, rg_error = _search_with_rg(query, target, bool(regex), bool(case_sensitive))
    backend = "rg"
    if rg_error == "missing":
        matches, py_error = _search_with_python(query, target, bool(regex), bool(case_sensitive))
        backend = "python"
        if py_error:
            return json.dumps({"error": py_error, "backend": backend}, ensure_ascii=False)
    elif rg_error:
        return json.dumps({"error": rg_error, "backend": backend}, ensure_ascii=False)

    matches = sorted(matches, key=lambda item: (_norm_sort_key(item[0]), item[1]))
    total = len(matches)
    result = {
        "query": query,
        "path": _clean_relpath(target),
        "regex": bool(regex),
        "case_sensitive": bool(case_sensitive),
        "limit": limit_int,
        "context": context_int,
        "backend": backend,
        "total": total,
        "hits": _format_code_hits(matches, limit_int, context_int),
        "truncated": total > limit_int,
    }
    if not result["hits"]:
        result["message"] = "没有找到匹配代码。"
    return json.dumps(result, ensure_ascii=False)


def _identifier_target(name: str) -> tuple[str, str] | tuple[None, str]:
    symbol = name.strip()
    if not symbol:
        return None, "name 不能为空"
    identifier = symbol.rsplit(".", 1)[-1]
    if not identifier.isidentifier():
        return None, f"name 必须是 Python 标识符或点分限定名：{name}"
    return identifier, ""


def _kind_mode(kind: str) -> tuple[bool, bool, set[str] | None, str | None]:
    normalized = (kind or "both").strip().lower()
    if normalized in ("both", "all"):
        return True, True, None, None
    if normalized in ("definition", "definitions", "def", "defs"):
        return True, False, None, None
    if normalized in ("reference", "references", "ref", "refs"):
        return False, True, None, None
    if normalized in ("function", "class", "constant"):
        return True, False, {normalized}, None
    return False, False, None, "kind 只能是 both/definition/reference/function/class/constant"


def _target_names(target: ast.AST) -> list[str]:
    if isinstance(target, ast.Name):
        return [target.id]
    if isinstance(target, (ast.Tuple, ast.List)):
        names = []
        for item in target.elts:
            names.extend(_target_names(item))
        return names
    return []


def _attribute_name(node: ast.AST) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        parent = _attribute_name(node.value)
        return f"{parent}.{node.attr}" if parent else node.attr
    return None


class _SymbolVisitor(ast.NodeVisitor):
    def __init__(
        self,
        symbol: str,
        identifier: str,
        lines: list[str],
        include_definitions: bool,
        include_references: bool,
        definition_kinds: set[str] | None,
    ):
        self.symbol = symbol
        self.identifier = identifier
        self.lines = lines
        self.include_definitions = include_definitions
        self.include_references = include_references
        self.definition_kinds = definition_kinds
        self.scope: list[str] = []
        self.hits: list[dict] = []

    def _snippet(self, line_number: int) -> str:
        if 1 <= line_number <= len(self.lines):
            return _strip_line(self.lines[line_number - 1])
        return ""

    def _definition_matches(self, local_name: str, qualified_name: str) -> bool:
        if "." in self.symbol:
            return qualified_name == self.symbol
        return local_name == self.identifier

    def _reference_matches_name(self, local_name: str) -> bool:
        return "." not in self.symbol and local_name == self.identifier

    def _reference_matches_attribute(self, node: ast.Attribute) -> bool:
        dotted = _attribute_name(node)
        if "." in self.symbol:
            return dotted == self.symbol
        return node.attr == self.identifier

    def _add_hit(self, role: str, kind: str, name: str, qualified_name: str, node: ast.AST) -> None:
        if role == "definition" and self.definition_kinds and kind not in self.definition_kinds:
            return
        self.hits.append({
            "role": role,
            "kind": kind,
            "name": name,
            "qualified_name": qualified_name,
            "scope": ".".join(self.scope) or "<module>",
            "line": getattr(node, "lineno", 0),
            "column": getattr(node, "col_offset", 0) + 1,
            "snippet": self._snippet(getattr(node, "lineno", 0)),
        })

    def _visit_function(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        qualified_name = ".".join(self.scope + [node.name])
        if self.include_definitions and self._definition_matches(node.name, qualified_name):
            self._add_hit("definition", "function", node.name, qualified_name, node)
        self.scope.append(node.name)
        self.generic_visit(node)
        self.scope.pop()

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._visit_function(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self._visit_function(node)

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        qualified_name = ".".join(self.scope + [node.name])
        if self.include_definitions and self._definition_matches(node.name, qualified_name):
            self._add_hit("definition", "class", node.name, qualified_name, node)
        self.scope.append(node.name)
        self.generic_visit(node)
        self.scope.pop()

    def visit_Assign(self, node: ast.Assign) -> None:
        if not self.scope and self.include_definitions:
            for target in node.targets:
                for name in _target_names(target):
                    if self._definition_matches(name, name):
                        self._add_hit("definition", "constant", name, name, node)
        self.visit(node.value)

    def visit_AnnAssign(self, node: ast.AnnAssign) -> None:
        if not self.scope and self.include_definitions:
            for name in _target_names(node.target):
                if self._definition_matches(name, name):
                    self._add_hit("definition", "constant", name, name, node)
        if node.annotation:
            self.visit(node.annotation)
        if node.value:
            self.visit(node.value)

    def visit_AugAssign(self, node: ast.AugAssign) -> None:
        if not self.scope and self.include_definitions:
            for name in _target_names(node.target):
                if self._definition_matches(name, name):
                    self._add_hit("definition", "constant", name, name, node)
        self.visit(node.value)

    def visit_Name(self, node: ast.Name) -> None:
        if self.include_references and isinstance(node.ctx, ast.Load) and self._reference_matches_name(node.id):
            self._add_hit("reference", "name", node.id, node.id, node)

    def visit_Attribute(self, node: ast.Attribute) -> None:
        if self.include_references and isinstance(node.ctx, ast.Load) and self._reference_matches_attribute(node):
            self._add_hit("reference", "attribute", node.attr, _attribute_name(node) or node.attr, node)
        self.generic_visit(node)


@tool
def search_symbol(name: str, path: str = "", kind: str = "both", limit: int = _DEFAULT_LIMIT) -> str:
    """在工作目录内扫描 Python AST，查找函数/类/顶层常量定义与同名引用。

    kind 可取 both、definition、reference，也可用 function/class/constant 只看某类定义。
    语法错误文件会被跳过并在 skipped 中列出，不会中断整个扫描。
    """
    if not isinstance(name, str):
        return json.dumps({"error": "name 必须是字符串"}, ensure_ascii=False)
    symbol = name.strip()
    identifier, error = _identifier_target(symbol)
    if error:
        return json.dumps({"error": error}, ensure_ascii=False)
    include_definitions, include_references, definition_kinds, error = _kind_mode(kind)
    if error:
        return json.dumps({"error": error}, ensure_ascii=False)
    limit_int, error = _bounded_int(limit, _DEFAULT_LIMIT, 1, _MAX_LIMIT, "limit")
    if error:
        return json.dumps({"error": error}, ensure_ascii=False)

    try:
        target = _safe_target(path)
    except ValueError as e:
        return json.dumps({"error": str(e)}, ensure_ascii=False)
    if not os.path.exists(target):
        return json.dumps({"error": f"路径不存在：{path or '.'}"}, ensure_ascii=False)

    hits: list[dict] = []
    skipped: list[dict] = []
    for full_path in _iter_python_files(target):
        rel_path = _clean_relpath(full_path)
        try:
            lines = _read_file_lines(full_path)
            tree = ast.parse("".join(lines), filename=rel_path, type_comments=True)
        except SyntaxError as e:
            skipped.append({
                "file": rel_path,
                "error": f"SyntaxError: {e.msg}",
                "line": e.lineno,
            })
            continue
        except OSError as e:
            skipped.append({"file": rel_path, "error": f"读取失败：{e}"})
            continue

        visitor = _SymbolVisitor(
            symbol=symbol,
            identifier=identifier,
            lines=lines,
            include_definitions=include_definitions,
            include_references=include_references,
            definition_kinds=definition_kinds,
        )
        visitor.visit(tree)
        for hit in visitor.hits:
            hit["file"] = rel_path
            hits.append(hit)

    role_order = {"definition": 0, "reference": 1}
    hits.sort(key=lambda h: (
        role_order.get(h["role"], 9),
        os.path.normcase(h["file"]),
        h["line"],
        h["column"],
        h["qualified_name"],
    ))
    total = len(hits)
    result = {
        "name": symbol,
        "path": _clean_relpath(target),
        "kind": kind,
        "limit": limit_int,
        "total": total,
        "hits": hits[:limit_int],
        "truncated": total > limit_int,
        "skipped": skipped,
    }
    if not result["hits"]:
        result["message"] = "没有找到匹配符号。"
    return json.dumps(result, ensure_ascii=False)


@tool
def read_code(path: str, start_line: int, end_line: int) -> str:
    """按 1-based 闭区间读取工作目录内代码片段，返回带行号的文本。

    超过 200 行的请求会从 start_line 起截断到 200 行，避免结果过大。
    """
    try:
        start = int(start_line)
        end = int(end_line)
    except (TypeError, ValueError):
        return json.dumps({"error": "start_line 和 end_line 必须是整数"}, ensure_ascii=False)
    if start < 1:
        return json.dumps({"error": "start_line 必须大于等于 1"}, ensure_ascii=False)
    if end < start:
        return json.dumps({"error": "end_line 必须大于等于 start_line"}, ensure_ascii=False)

    try:
        full_path = _safe_target(path)
    except ValueError as e:
        return json.dumps({"error": str(e)}, ensure_ascii=False)
    if not os.path.isfile(full_path):
        return json.dumps({"error": f"文件不存在：{path}"}, ensure_ascii=False)
    if not _is_searchable_file(full_path):
        return json.dumps({"error": f"文件不是可读取的文本代码文件：{path}"}, ensure_ascii=False)

    try:
        lines = _read_file_lines(full_path)
    except OSError as e:
        return json.dumps({"error": f"读取失败：{e}"}, ensure_ascii=False)

    total_lines = len(lines)
    if start > total_lines:
        return json.dumps({
            "error": f"start_line 超出文件范围：{start}",
            "total_lines": total_lines,
        }, ensure_ascii=False)
    if end > total_lines:
        return json.dumps({
            "error": f"end_line 超出文件范围：{end}",
            "total_lines": total_lines,
        }, ensure_ascii=False)

    requested_end = end
    truncated = False
    if end - start + 1 > _MAX_READ_LINES:
        end = start + _MAX_READ_LINES - 1
        truncated = True

    width = len(str(end))
    content = "\n".join(
        f"{idx:>{width}} | {_strip_line(lines[idx - 1])}"
        for idx in range(start, end + 1)
    )
    return json.dumps({
        "path": _clean_relpath(full_path),
        "start_line": start,
        "end_line": end,
        "requested_end_line": requested_end,
        "total_lines": total_lines,
        "truncated": truncated,
        "content": content,
    }, ensure_ascii=False)
