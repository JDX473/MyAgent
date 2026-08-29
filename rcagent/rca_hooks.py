"""RCA 专有钩子:错误处理(重复调用检测 / 过早 finalize 打回) + finalize 终止信号。

本模块被 rcagent/__init__.py import 时把钩子注册进全局 hooks 单例
(仿 tools/hooks_setup.py) —— 只在 main_rca.py 垂直入口挂载,
不影响 `python main.py` 的通用行为。

设计要点:
  - 用 POST_TOOL_EXECUTE 钩子改写 ctx.result —— loop 回填 messages 用的是
    ctx.result(见 core/loop.py 的配套改动),因此钩子能直接决定模型"看到"什么;
  - finalize_done 置 ctx.state['_agent_finished'],loop 检测到后主动终结算段,
    使 finalize 成为真正的结构化出口而非靠模型自觉停。
"""
import json

from core.hooks import HookContext, HookEvents, hooks
from rcagent.context_mgr import RCA_SESSION_NAME

# 可累积"调查/分析工作量"的工具:finalize 前至少出现 _MIN_SURVEY 次
_SURVEY_TOOLS = {
    "list_entities", "query_logs", "get_entity_detail",
    "get_snapshot", "analyze_logs", "read", "websearch",
}
# finalize 前至少要执行的采集/分析次数(防过早收尾)
_MIN_SURVEY = 2
# 重复调用检测窗口(最近 N 次工具调用)
_DUP_WINDOW = 20
# 不做重复检测的工具(finally / 计划工具)
_NO_DUP_CHECK = {"finalize", "plan_task", "update_step", "revise_plan", "get_plan"}
# tool_history 上限,防长会话里无限膨胀
_HISTORY_MAX = 500


def _parse_payload(raw: str) -> tuple[dict | None, object]:
    """解 run_tool 包装的 {"result": <str或dict>}:返回 (outer, inner)。

    inner 可能是 dict(如 finalize 报告)、str 或 None。
    """
    try:
        outer = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return None, None
    if not isinstance(outer, dict):
        return outer, None
    inner = outer.get("result")
    if isinstance(inner, str):
        try:
            inner = json.loads(inner)
        except (json.JSONDecodeError, TypeError):
            pass
    return outer, inner


def _format_report(rep: dict) -> str:
    resp = rep.get("responsibility")
    resp_text = "用户侧(user)" if resp == "user" else "平台侧(platform)"
    return "\n".join([
        "【根因定位报告】",
        f"根因: {rep.get('root_cause', '')}",
        f"解决方案: {rep.get('solution', '')}",
        f"证据: {rep.get('evidence', '')}",
        f"责任归属: {resp_text}",
        f"置信度: {rep.get('confidence', '')}",
    ])


def dup_call_detector(ctx: HookContext) -> None:
    """POST_TOOL_EXECUTE:记录工具调用历史;窗口内同参数重复调用 -> 改写结果为错误。

    ⚠️ 只能用"改写 ctx.result"(工具结果即错误),【不能】向 ctx.messages 插入
    system 消息——那会打断 assistant(tool_calls) → tool 回应的配对,触发
    DeepSeek API 400(实测)。模型的纠错靠看到工具错误结果自己调整。
    """
    if ctx.name != RCA_SESSION_NAME:
        return
    if ctx.tool_name in _NO_DUP_CHECK:
        return
    history = ctx.state.setdefault("tool_history", [])
    history.append((ctx.tool_name, ctx.arguments))
    if len(history) > _HISTORY_MAX:
        del history[:len(history) - _HISTORY_MAX]
    if history[-_DUP_WINDOW:].count((ctx.tool_name, ctx.arguments)) >= 2:
        ctx.result = json.dumps({
            "error": (f"系统检测到:工具 <{ctx.tool_name}> 已用相同参数调用过,"
                      "这是重复无效调用。请停止重复,基于已有信息换思路或换参数。"),
        }, ensure_ascii=False)


def premature_finalize_guard(ctx: HookContext) -> None:
    """POST_TOOL_EXECUTE:finalize 时校验"调查充分性";证据不足则打回。

    打回方式:改写 ctx.result 为错误 JSON,且不置 _agent_finished
    → 模型看到拦截理由并继续调查,而非终结。
    """
    if ctx.name != RCA_SESSION_NAME:
        return
    if ctx.tool_name != "finalize":
        return
    _, inner = _parse_payload(ctx.result)
    if not isinstance(inner, dict):
        return  # 解析不到 → 交给模型自行读取错误
    if "error" in inner:
        return  # finalize 已自我校验失败,无需二次拦截
    report = inner.get("result")
    if not isinstance(report, dict):
        return
    survey = sum(1 for t, _ in ctx.state.get("tool_history", []) if t in _SURVEY_TOOLS)
    if survey >= _MIN_SURVEY:
        return
    if bool(report.get("acknowledge")):
        ctx.state["rca_acknowledged"] = True  # 自认证据不足 → 放行并记录
        return
    ctx.result = json.dumps({
        "error": (f"finalize 被系统拦截:目前仅做过 {survey} 次数据采集/分析"
                  f"(至少需要 {_MIN_SURVEY} 次)。请先调用 list_entities / "
                  f"query_logs / analyze_logs 等工具充分调查后再宣布结论。"),
    }, ensure_ascii=False)


def finalize_done(ctx: HookContext) -> None:
    """POST_TOOL_EXECUTE:finalize 成功时,保存报告并置终止信号。

    只在报告四要素齐全时置 _agent_finished;被 premature_finalize_guard
    打回(或 finalize 自我校验失败)时不会终止。
    """
    if ctx.name != RCA_SESSION_NAME:
        return
    if ctx.tool_name != "finalize":
        return
    _, inner = _parse_payload(ctx.result)
    if not isinstance(inner, dict) or "result" not in inner:
        return
    report = inner["result"]
    if not isinstance(report, dict):
        return
    if not all(report.get(k) for k in ("root_cause", "solution", "evidence", "responsibility")):
        return
    ctx.state["rca_report_raw"] = report
    ctx.state["rca_report"] = _format_report(report)
    ctx.state["rca_acknowledged"] = ctx.state.get("rca_acknowledged", False) or bool(
        report.get("acknowledge"))
    ctx.state["_agent_finished"] = True


# ----------------------------------------------------------------------
# 注册(import 本模块即生效,仿 tools/hooks_setup.py)
# ----------------------------------------------------------------------
_REGISTERED = False


def register_rca_hooks() -> None:
    """注册 RCA 专有钩子(幂等)。"""
    global _REGISTERED
    if _REGISTERED:
        return
    hooks.register(HookEvents.POST_TOOL_EXECUTE, dup_call_detector, priority=0)
    hooks.register(HookEvents.POST_TOOL_EXECUTE, premature_finalize_guard, priority=10)
    hooks.register(HookEvents.POST_TOOL_EXECUTE, finalize_done, priority=20)
    from rcagent.context_mgr import auto_snapshot
    hooks.register(HookEvents.POST_TOOL_EXECUTE, auto_snapshot, priority=100)
    _REGISTERED = True