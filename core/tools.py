"""工具注册表 + 执行白名单：@tool 装饰器 + 自动生成 schema，扩展只需写普通函数。

注册规则（由装饰器自动完成，无需手动填 JSON）：
   函数名        -> 工具名
   函数 docstring -> 工具描述（可选）
   参数的类型注解 -> parameters schema（必填）
安全约束：@tool 注册时会同步加入"执行白名单"；不在白名单内的工具
   即使被模型点名也不会执行（run_tool 与权限层双重拦截）。
"""
import inspect
import json
from typing import get_type_hints

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


def _type_to_json(t: type) -> str | dict:
    """把 Python 类型映射为 JSON Schema 的类型描述。

    返回 str（如 "string"）或 dict（如 {"type": "array", "items": ...}）。
    支持 int/float/str/bool 及 list[str]；其它类型兜底为 string。
    """
    if t == list:
        return "string"  # 裸 list（元素类型未知）兜底
    origin = getattr(t, "__origin__", None)
    if origin is list:
        return {"type": "array", "items": {"type": _type_to_json(t.__args__[0])}}
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
        type_desc = _type_to_json(t)
        if isinstance(type_desc, dict):
            properties[param_name] = type_desc
        else:
            properties[param_name] = {"type": type_desc}
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
