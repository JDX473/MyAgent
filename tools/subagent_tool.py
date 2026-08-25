"""subAgent 工具：把子任务委派给一个独立的嵌套 Agent。

设计（经用户确认）：
  - subAgent 是一个普通 @tool，主 Agent 自主决定何时调用。
  - subAgent 内部就是 AgentSession()：复用同一个 loop + 同一套全局 hooks，
    因此 6 个 hook 事件（含权限 deny/confirm）在 subAgent 里照样生效。
  - subAgent 没有 plan 工具（task 拆解由主 Agent 做），也没有 subAgent 工具
    （防无限递归，嵌套深度固定为 1）。
  - 结果超长时不直接截断：先让 subAgent 用自己的模型能力 summary（最多 2 次），
    仍超长才截断兜底——避免截断丢失关键信息。
"""
import json
import os

from core.tools import _TOOL_WHITELIST, tool

# subAgent 内不可用的工具：4 个计划工具 + subagent 自身（防递归）
_SUBAGENT_EXCLUDED = {"plan_task", "update_step", "revise_plan", "get_plan", "subagent"}
# 结果长度阈值：超过则触发 summary
_RESULT_MAX = 2000
# summary 最多尝试次数：之后仍超长才截断
_SUMMARY_MAX = 2
# subAgent 自己的轮数预算
_SUB_MAX_STEPS = 12


def _subagent_allowed() -> set[str]:
    """构建 subAgent 的允许工具集（延迟取白名单，避免 import 时序问题）。"""
    return {name for name in _TOOL_WHITELIST if name not in _SUBAGENT_EXCLUDED}


def _summarize(sess, answer: str, allowed: set[str]) -> str:
    """让 subAgent 压缩自己的超长回复；返回压缩后的文本。"""
    print(f"[{sess.name}] 结果过长（{len(answer)} 字符），让 subAgent 自己总结…")
    try:
        return sess.chat(
            "请将你刚才的完整回复压缩成要点总结，保留所有关键信息与结论，"
            f"控制在 {_RESULT_MAX} 字符以内。只输出总结本身。",
            max_steps=5,
            allowed_tools=allowed,
        )
    except Exception as e:
        # summary 失败不致命：返回原文让上层兜底截断
        print(f"[{sess.name}] 总结失败：{e}")
        return answer


@tool
def subagent(task: str, name: str = "subAgent") -> str:
    """把一个子任务委派给一个独立的 subAgent 执行，返回其最终结果。

    适合：独立、可隔离的子任务（如查询资料、处理单个文件、独立计算）。
    subAgent 内部也是一个 Agent Loop，可用大多数工具，但【不能】拆解任务
    计划、也不能再调用 subAgent（避免无限嵌套）。subAgent 的中间过程不会
    污染主上下文，只把最终结果返回给你。

    task：要交给 subAgent 的完整任务描述。
    name：必须由你（调用方）根据该子任务的实际职责取一个有意义的名字，
          用英文小写短横线风格，例如查资料 → "web-researcher"、改文件 →
          "file-editor"、算数据 → "data-calculator"、搜网页 → "web-searcher"。
          该名字会作为该 subAgent 所有控制台输出的前缀，方便区分是哪个
          Agent 在干活。不要使用默认值，请总是根据 task 给出贴合的名字。
    """
    if not isinstance(task, str) or not task.strip():
        return json.dumps({"error": "task 必须是非空字符串"}, ensure_ascii=False)
    name = name if isinstance(name, str) and name.strip() else "subAgent"

    from core.loop import AgentSession  # 延迟导入：避免模块初始化期的循环依赖

    allowed = _subagent_allowed()
    sess = AgentSession(name=name)
    print(f"[{name}] ==== 新子会话 · 委派任务 ====")
    print(f"[{name}] 子任务：{task}")

    try:
        answer = sess.chat(task, max_steps=_SUB_MAX_STEPS, allowed_tools=allowed)
    except Exception as e:
        return json.dumps({"error": f"subAgent 执行失败：{e}"}, ensure_ascii=False)

    # 预算用尽未产出任何结果 → 显式报错，不让主 Agent 拿到空结果
    if not answer:
        return json.dumps({"error": f"subAgent「{name}」在 {_SUB_MAX_STEPS} 轮内未产出结果（可能一直调用工具或预算不足）"},
                           ensure_ascii=False)

    # 超长 → summary（最多 2 次）→ 仍超长才截断兜底
    # 注意：若某次 summary 返回空（未保护原内容），保留截断前的原始回复，
    # 避免把超长结果整个丢空。
    for _ in range(_SUMMARY_MAX):
        if len(answer) <= _RESULT_MAX:
            break
        summarized = _summarize(sess, answer, allowed)
        if not summarized:
            print(f"[{name}] 总结返回空，保留原始回复并截断兜底")
            break
        answer = summarized

    if len(answer) > _RESULT_MAX:
        print(f"[{name}] 两次总结后仍超长（{len(answer)} 字符），截断兜底")
        answer = answer[:_RESULT_MAX] + "...(已截断)"

    return json.dumps({"result": answer}, ensure_ascii=False)
