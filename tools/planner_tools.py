"""任务计划工具：plan_task / update_step / revise_plan / get_plan。

多步骤任务先拆成 step 再逐步执行，跟踪每个 step 的状态，
防止模型在长任务中目标漂移。状态机本身在 core/planner.py（纯函数）。

说明：
  - 这四个工具注册即入执行白名单，权限层直接放行（allow，不弹确认）。
  - 计划的持久化挂在 ctx.state['plan'] 上：随会话跨轮存活，/clear 自动丢弃。
  - ctx 绑定：loop 在每次执行工具前调用 planner.bind(ctx)，工具内部才能
    读到 ctx.state（run_tool 本身不传 ctx，这是唯一的接线点）。
"""
import json

from core import planner as planner_core
from core.hooks import HookContext
from core.tools import tool

_PLAN_KEY = "plan"

# 允许的目标状态（工具侧校验；真正的转移规则在 planner.validate_transition）
_STATUS_ALIAS = {
    "pending": planner_core.PENDING,
    "未执行": planner_core.PENDING,
    "in_progress": planner_core.IN_PROGRESS,
    "执行中": planner_core.IN_PROGRESS,
    "done": planner_core.DONE,
    "执行完成": planner_core.DONE,
    "failed": planner_core.FAILED,
    "执行失败": planner_core.FAILED,
    "skipped": planner_core.SKIPPED,
    "跳过": planner_core.SKIPPED,
}

# 当前会话的 HookContext 引用（由 loop 在每次 run_tool 前 bind 进来）
_current_ctx: HookContext | None = None


def bind(ctx: HookContext) -> None:
    """把当前会话上下文绑给本模块，使计划工具能读写 ctx.state['plan']。

    loop.py 在调用 run_tool 前调用一次；无 plan 时直接绑定，无副作用。
    """
    global _current_ctx
    _current_ctx = ctx


def _get_ctx() -> HookContext | None:
    return _current_ctx


def _plan() -> dict | None:
    """读取当前会话的活跃计划；没有则返回 None。"""
    ctx = _get_ctx()
    if ctx is None:
        return None
    return ctx.state.get(_PLAN_KEY)


def _ok(msg: str) -> str:
    return json.dumps({"ok": True, "message": msg}, ensure_ascii=False)


def _err(msg: str) -> str:
    return json.dumps({"error": msg}, ensure_ascii=False)


@tool
def plan_task(steps: list[str]) -> str:
    """把一个多步骤任务拆分为多个 step，创建/替换当前任务计划。

    调用后必须按顺序逐步执行：每完成一个 step 调用 update_step 更新状态，
    全部完成后任务才算结束。单步即可完成的任务不需要调用本工具。
    steps：step 标题列表，按执行顺序给出（建议 2~6 步）。
    """
    if not steps or not all(str(s).strip() for s in steps):
        return _err("steps 不能为空，且每个 step 都要有标题")
    plan = planner_core.new_plan([str(s).strip() for s in steps])
    ctx = _get_ctx()
    if ctx is None:
        return _err("当前会话未绑定上下文，无法保存计划")
    ctx.state[_PLAN_KEY] = plan
    return _ok(f"计划已创建，共 {len(plan['steps'])} 个 step。\n"
               + planner_core.serialize_plan(plan))


@tool
def update_step(step_index: int, status: str, note: str = "", reason: str = "") -> str:
    """更新计划中某个 step 的状态，并给出说明。

    step_index：step 编号（从 0 开始，见 plan_task 返回的计划）。
    status：目标状态，支持 未执行(pending) / 执行中(in_progress) /
            执行完成(done) / 执行失败(failed) / 跳过(skipped)。
    note：标记"执行完成"时必须填写，说明做了什么、如何验证。
    reason：标记"执行失败"或"跳过"时必须填写原因。
    系统会强制校验状态转移合法性：不允许跳步、不允许同时推进多个 step、
    执行完成必须带 note 等。非法转移会返回错误，请按提示调整。
    """
    plan = _plan()
    if plan is None:
        return _err("当前没有活跃计划，请先调用 plan_task 创建计划")
    target = _STATUS_ALIAS.get(str(status).strip().lower())
    if target is None:
        return _err(f"未知状态：{status}。支持：未执行/执行中/执行完成/执行失败/跳过")
    ok, new_plan, msg = planner_core.apply_transition(
        plan, step_index, target, note=note, reason=reason)
    if not ok:
        return _err(msg)
    _current_ctx.state[_PLAN_KEY] = new_plan  # 就地更新（_plan() 非 None 说明 ctx 已绑定）
    return _ok(msg + "。\n" + planner_core.serialize_plan(new_plan, compact=True))


@tool
def revise_plan(steps: list[str]) -> str:
    """整体修订计划：新增 / 删除 / 重排 step，并重新开始执行顺序。

    当执行中发现原计划不再合适（如某 step 被权限拒绝、需换实现方式）
    时调用。steps：新的 step 标题列表（按新的执行顺序）。
    已终结的 step 若标题不变会保留原状态，其余重置为未执行。
    """
    if not steps or not all(str(s).strip() for s in steps):
        return _err("steps 不能为空")
    ctx = _get_ctx()
    if ctx is None:
        return _err("当前会话未绑定上下文，无法修订计划")
    old = ctx.state.get(_PLAN_KEY)
    plan = planner_core.revise_plan(old, [str(s).strip() for s in steps]) if old \
        else planner_core.new_plan([str(s).strip() for s in steps])
    ctx.state[_PLAN_KEY] = plan
    return _ok(f"计划已修订，共 {len(plan['steps'])} 个 step。\n"
               + planner_core.serialize_plan(plan))


@tool
def get_plan() -> str:
    """查看当前任务计划的完整状态（各 step 的执行状态、说明）。

    适用于多轮会话里的后续追问（如"继续"、"还差什么"），
    或在计划执行中断后重新确认剩余步骤。
    """
    plan = _plan()
    if plan is None:
        return _err("当前没有活跃计划。")
    text = planner_core.serialize_plan(plan)
    if planner_core.plan_done(plan):
        text += "\n计划已全部完成。"
    else:
        rest = planner_core.summarize_incomplete(plan)
        if rest:
            text += "\n" + rest
    return _ok(text)
