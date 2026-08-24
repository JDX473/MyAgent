"""任务计划状态机（纯函数，无 I/O，便于单测）。

解决长期任务的目标漂移问题：把任务拆成 step，跟踪每个 step 的状态，
只允许合法的状态转移，并提供一个紧凑的快照用于定期提醒模型。

状态（五态，比"未执行/执行中/执行完成"多出 failed / skipped，
因为三态在"执行中被权限拒绝/出错"时没有合法出口，会死锁整个计划）：

  未执行   pending     尚未开始
  执行中   in_progress 正在执行（同一时刻至多一个）
  执行完成 done        完成（必须带 note；且最近一次工具结果不得是 blocked）
  执行失败 failed      出错（必须带 reason；可重试或跳过或改计划）
  跳过     skipped     模型/用户判定无需执行

转移规则（由 apply_transition 系统侧强制，不信任模型自报）：
  pending      -> in_progress   仅当它是"前沿"（此前所有 step 已终结），且无其它 step 在执行中
  pending      -> skipped       可任意跳过（带 reason）
  pending      -> failed        不允许直接跳转（未执行过就失败没有意义）
  in_progress  -> done          必须带 note
  in_progress  -> failed        必须带 reason
  in_progress  -> pending       重试（attempts 上限内；上限后只能 failed/skip/revise）
  failed       -> pending       重试（attempts 上限内）
  failed       -> skipped       跳过（带 reason）
  failed       -> done          覆盖标记完成（override，必须带 note）
  done/skipped 均为终结态，只能通过 revise_plan 变更

计划生命周期：plan 由模型调用 plan_task 创建/替换（单活），
随 /clear 新建会话自动丢弃；revise_plan 整体替换 step 列表。
"""
import json

# ---- 状态常量 ----
PENDING = "pending"        # 未执行
IN_PROGRESS = "in_progress"  # 执行中
DONE = "done"              # 执行完成
FAILED = "failed"          # 执行失败
SKIPPED = "skipped"        # 跳过

TERMINAL = (DONE, SKIPPED)   # 终结态：仅能通过 revise 变更
MAX_ATTEMPTS = 3             # 单个 step 重试上限

# 系统侧校验的合法转移表：source -> 允许的 targets
_TRANSITIONS: dict[str, tuple[str, ...]] = {
    PENDING: (IN_PROGRESS, SKIPPED),
    IN_PROGRESS: (DONE, FAILED, PENDING),
    FAILED: (PENDING, SKIPPED, DONE),
    DONE: (),
    SKIPPED: (),
}


def new_plan(steps: list[str]) -> dict:
    """根据 step 标题列表创建一份新计划（初始全部 pending）。"""
    return {
        "steps": [
            {"id": i, "title": title, "status": PENDING,
             "note": "", "reason": "", "attempts": 0}
            for i, title in enumerate(steps)
        ],
        "version": 1,
    }


def _is_terminal(step: dict) -> bool:
    return step["status"] in TERMINAL


def _active_step_index(plan: dict) -> int | None:
    """当前正在执行中的 step 下标；没有则返回 None。"""
    for i, s in enumerate(plan["steps"]):
        if s["status"] == IN_PROGRESS:
            return i
    return None


def _frontier_index(plan: dict) -> int:
    """下一个应该执行的 step（前沿）：第一个非终结态的 step。"""
    for i, s in enumerate(plan["steps"]):
        if s["status"] not in TERMINAL:
            return i
    return len(plan["steps"])  # 全部终结


def validate_transition(plan: dict, index: int, target: str,
                        *, note: str = "", reason: str = "") -> str | None:
    """校验 index 处 step 转移到 target 是否合法。

    合法返回 None；不合法返回可读的中文错误说明（返回给模型，便于纠正）。
    """
    steps = plan["steps"]
    if index < 0 or index >= len(steps):
        return f"step 下标 {index} 超出范围（共 {len(steps)} 个 step）"
    step = steps[index]
    source = step["status"]
    if target not in _TRANSITIONS.get(source, ()):
        return (f"非法转移：{source} -> {target}（step {index}）。"
                f"允许的目标：{', '.join(_TRANSITIONS.get(source, ())) or '无（终结态，请用 revise_plan）'}")
    if target == IN_PROGRESS:
        active = _active_step_index(plan)
        if active is not None and active != index:
            return f"已有 step {active} 在执行中，不能同时推进多个 step"
        if not all(_is_terminal(s) for s in steps[:index]):
            return f"step {index} 不是前沿：请先完成前面的 step（顺序推进）"
    if target == DONE and not note.strip():
        return "标记执行完成必须提供 note（说明做了什么、如何验证）"
    if target in (FAILED, SKIPPED) and not reason.strip():
        return f"标记 {target} 必须提供 reason"
    if target == DONE and source == FAILED and not note.strip():
        return "从执行失败覆盖标记完成必须提供 note"
    if target == PENDING and step["attempts"] >= MAX_ATTEMPTS:
        return f"该 step 已重试 {MAX_ATTEMPTS} 次，请改为失败/跳过或调用 revise_plan 调整计划"
    return None


def apply_transition(plan: dict, index: int, target: str,
                     *, note: str = "", reason: str = "") -> tuple[bool, dict, str]:
    """执行一次状态转移（含校验）。

    返回 (是否成功, plan, 说明)。成功时 plan 已就地更新；
    失败时 plan 不变，说明为可读的错误信息（返回给模型纠正）。
    """
    err = validate_transition(plan, index, target, note=note, reason=reason)
    if err:
        return False, plan, err

    steps = plan["steps"]
    step = steps[index]
    if target == IN_PROGRESS:
        # 退出原先的执行中（若有）
        for s in steps:
            if s["status"] == IN_PROGRESS:
                s["status"] = PENDING  # 让位（理论上不会发生，双保险）
        step["status"] = IN_PROGRESS
        step["attempts"] += 1  # 进入执行中计一次尝试
        step["note"] = ""
        step["reason"] = ""
    elif target == DONE:
        step["status"] = DONE
        step["note"] = note
        step["reason"] = ""
    elif target == FAILED:
        step["status"] = FAILED
        step["reason"] = reason
        step["note"] = ""
    elif target == PENDING:
        # 重试（in_progress/failed -> pending）
        step["status"] = PENDING
        step["note"] = ""
        step["reason"] = ""
    elif target == SKIPPED:
        step["status"] = SKIPPED
        step["reason"] = reason
        step["note"] = ""

    plan["version"] += 1
    return True, plan, f"step {index} 已更新为 {target}"


def revise_plan(plan: dict, steps: list[str]) -> dict:
    """整体替换计划（新增/删除/重排 step）。已终结 step 尽量保留状态。

    同名 step 沿用原状态；其余重置为 pending。版本号递增。
    """
    old_by_title = {s["title"]: s for s in plan["steps"]}
    new_steps = []
    for i, title in enumerate(steps):
        old = old_by_title.get(title)
        if old is not None:
            new_steps.append(dict(old))
            new_steps[-1]["id"] = i
        else:
            new_steps.append(
                {"id": i, "title": title, "status": PENDING,
                 "note": "", "reason": "", "attempts": 0})
    return {"steps": new_steps, "version": plan["version"] + 1}


def _status_text(status: str) -> str:
    return {
        PENDING: "未执行",
        IN_PROGRESS: "执行中",
        DONE: "执行完成",
        FAILED: "执行失败",
        SKIPPED: "跳过",
    }.get(status, status)


def serialize_plan(plan: dict, compact: bool = False) -> str:
    """把计划序列化为紧凑文本（供注入上下文/提醒）。"""
    if not plan or not plan.get("steps"):
        return ""
    lines = ["[当前计划]"]
    for s in plan["steps"]:
        mark = {
            DONE: "[✓]",
            IN_PROGRESS: "[▶]",
            FAILED: "[✗]",
            SKIPPED: "[–]",
            PENDING: "[ ]",
        }.get(s["status"], "[ ]")
        suffix = ""
        if s.get("note"):
            suffix = f"  note: {s['note']}"
        elif s.get("reason"):
            suffix = f"  reason: {s['reason']}"
        if not compact:
            suffix = f"（{_status_text(s['status'])}）{suffix}"
        lines.append(f"{mark} {s['id']}. {s['title']}{suffix}")
    return "\n".join(lines)


def plan_done(plan: dict) -> bool:
    """计划是否全部终结（done/skipped）。"""
    return bool(plan) and all(s["status"] in TERMINAL for s in plan["steps"])


def summarize_incomplete(plan: dict) -> str:
    """返回未完成 step 的汇总（供 STOP 钩子输出）。"""
    if plan_done(plan):
        return ""
    unfinished = [
        f"{s['id']}. {s['title']}（{_status_text(s['status'])}）"
        for s in plan["steps"] if s["status"] not in TERMINAL
    ]
    if not unfinished:
        return ""
    return "计划未完成：" + "；".join(unfinished) + "。"


def plan_to_json(plan: dict) -> str:
    """把计划转成紧凑 JSON 字符串（工具返回值）。"""
    return json.dumps(plan, ensure_ascii=False)
