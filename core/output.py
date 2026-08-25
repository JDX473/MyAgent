"""统一输出通道：Agent 的控制台输出都走这里。

终端模式默认用 print 直出；TUI / 其它宿主模式可注入一个 sink 回调，
把每一行输出交给宿主渲染（如聊天式 TUI 的滚动消息区）。

这样 Agent 核心（loop / hooks / 工具）无需关心输出目标，
只需调用 output.emit(...)，由外部决定落到终端还是界面。
"""
import sys
import threading

# 默认 sink：直接 print 到 stdout
_default_sink = lambda text: print(text)

_sink = _default_sink
_lock = threading.Lock()


def set_sink(sink) -> None:
    """注入输出回调。sink(text: str)。传 None 恢复默认 print。"""
    global _sink
    with _lock:
        _sink = sink if sink is not None else _default_sink


def emit(text: str) -> None:
    """输出一行 Agent 交互文本。"""
    with _lock:
        sink = _sink
    try:
        sink(text)
    except Exception:
        # sink 出错时不致命：退回默认 print，避免打断 Agent 主流程
        _default_sink(text)


def is_default() -> bool:
    """当前是否仍是默认 print 模式（终端）。"""
    return _sink is _default_sink
