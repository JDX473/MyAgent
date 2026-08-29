"""轻量日志分析专家:把一段快照日志交给 subAgent 做深度根因分析。

对应 RCAgent(arXiv:2310.16340) 论文里"LLM 专家 Agent"的轻量实现
(v1 不做语义分块 + 向量检索,保持零依赖):

  - 直接用 subagent(嵌套 Agent Loop + 领域 prompt)读日志原文;
  - 要求以 JSON 输出 root_cause/solution/evidence,evidence 必须逐字引用原文;
  - 用"原文子串校验"拦幻觉:evidence 在原文里找不到 → 带反馈重试一次,
    仍失败则标注 verified=false,让控制器自行判断。

安全:纯只读分析,无副作用。
"""
import json

from core.tools import tool

from rcagent import context_mgr
from rcagent.stabilization import repair_json

_MAX_CHARS = 8000   # 一次交给专家的日志规模(防止把专家上下文也撑爆)
_MAX_TRIES = 2      # evidence 校验失败最多带反馈重试次数


def _verify_evidence(evidence: str, content: str) -> bool:
    """evidence 是否为 content 的(空白归一化后的)子串。逐字引用的证据必然命中。"""
    if not evidence or not content:
        return False
    e = " ".join(evidence.split())
    c = " ".join(content.split())
    return e in c


def _unwrap_subagent(raw: str) -> tuple[str | None, bool]:
    """解 subagent 工具的返回(外层 {"result": ...} 或 {"error": ...})。

    返回 (有效文本, 是否成功);失败时第一项为 None。
    """
    try:
        outer = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return raw, False
    if isinstance(outer, dict) and "error" in outer:
        return None, False
    if isinstance(outer, dict) and isinstance(outer.get("result"), str):
        return outer["result"], True
    return str(outer), True


def _extract_analysis(text: str) -> dict | None:
    """从专家输出文本里尽力提取 dict(带修复器兜底)。"""
    if not text:
        return None
    parsed = repair_json(text)
    if isinstance(parsed, dict):
        return parsed
    return None


def _build_task(text: str, question: str, feedback: str = "") -> str:
    fb = (
        f"\n\n上一轮你返回的 evidence 未能在日志原文中找到,禁止编造。"
        f"请基于上面日志原文重新提取确凿证据: {feedback}"
        if feedback else ""
    )
    return (
        "你是云系统根因分析专家。下面是某实体在故障窗口内的日志原文片段(节选)。\n"
        "请做根因分析,并【只输出一个 JSON 对象】(不要输出任何其它文字),字段:\n"
        '- "root_cause": 根因(str)\n'
        '- "solution": 缓解/修复建议(str)\n'
        '- "evidence": 支撑结论的证据——必须【逐字】摘抄自上面的日志原文,禁止改写或编造(str)\n'
        f"\n关注的问题: {question or '(根因是什么)'}\n"
        f"\n日志原文片段:\n```\n{text}\n```\n"
        f"{fb}"
    )


@tool
def analyze_logs(snapshot_key: str, question: str = "") -> str:
    """让日志分析专家对一份快照日志做深度根因分析,返回结构化的分析结果。

    snapshot_key: [snapshot: <key>] 标注的快照键(来自被压缩的长工具结果);
    question: 可选的分析关注点(返回 evidence 为逐字原文摘抄,已做真伪校验)。

    返回 JSON:
      analysis:  {root_cause, solution, evidence} 或无法结构化时的原始文本;
      evidence_verified: 证据是否通过原文校验(bool)。
    """
    content = context_mgr.snapshot_store().get(snapshot_key)
    if content is None:
        return json.dumps({"error": f"快照不存在(可能已被淘汰),key={snapshot_key}"},
                          ensure_ascii=False)
    text = content[:_MAX_CHARS]

    # 延迟导入,避免模块初始化期的循环依赖
    from tools.subagent_tool import subagent

    feedback = ""
    raw_result = None
    for attempt in range(_MAX_TRIES):
        task = _build_task(text, question, feedback=feedback)
        outer = subagent(task, name="log-expert")
        raw_result = outer
        text_ok, res = _unwrap_subagent(outer)
        if not text_ok or res is None:
            break  # 专家出错/无结果 → 把错误原样交回
        analysis = _extract_analysis(res)
        if analysis is None:
            # 专家没按 JSON 输出 → 不再重试,把原始文本交回(可能仍有用)
            return json.dumps({
                "analysis": res, "evidence_verified": False,
                "note": "专家输出未能解析为结构化 JSON,请阅读以上原文判断",
            }, ensure_ascii=False)
        ev = str(analysis.get("evidence") or "")
        if _verify_evidence(ev, content):
            return json.dumps({"analysis": analysis, "evidence_verified": True},
                              ensure_ascii=False)
        feedback = f"evidence={ev!r}"  # 带证据原文重新让专家提取
    # 重试耗尽仍未通过校验
    if isinstance(raw_result, str) and raw_result:
        _, res = _unwrap_subagent(raw_result)
    else:
        res = None
    return json.dumps({
        "analysis": res or "专家分析未能通过证据校验(可能为幻觉)",
        "evidence_verified": False,
        "note": f"经 {_MAX_TRIES} 次尝试仍无法在原文中核对到 evidence,结论需谨慎采信。",
    }, ensure_ascii=False)