"""RCA 垂直工具:信息采集(只读) + 快照取回 + finalize 结构化报告。

安全定位:工具集里【没有任何】写/改系统状态的操作 —— 只读采集 + 只读快照 +
纯文本报告。这是与项目既有安全姿态一致的红线:诊断全自动,处置留给人。

职责分工:
  - list_entities / query_logs / get_entity_detail  走数据源适配器(只读);
  - get_snapshot                                     取回 OBSK 压缩掉的全文片段;
  - analyze_logs                                     深度日志分析(见 log_expert.py);
  - finalize                                         结构化 RCA 报告出口(终止诊断)。
"""
import json

from core.tools import tool

from rcagent import context_mgr
from rcagent.adapters import get_adapter

# 责任归属归一表(自由文本 -> user/platform)
_RESPONSIBILITY_ALIAS = {
    "user": "user", "用户": "user", "用户责任": "user", "客户": "user",
    "客户端": "user", "使用者": "user", "用户侧": "user",
    "platform": "platform", "平台": "platform", "平台责任": "platform",
    "服务端": "platform", "服务端责任": "platform", "基础设施": "platform",
    "iaas": "platform", "paas": "platform",
}


def parse_responsibility(text: object) -> str | None:
    """把模型对责任归属的自由文本归一为 user / platform;无法判定返回 None。"""
    if not isinstance(text, str):
        return None
    norm = text.strip().lower()
    return _RESPONSIBILITY_ALIAS.get(norm)


@tool
def get_snapshot(snapshot_key: str, max_chars: int = 8000) -> str:
    """取回一个工具结果快照的片段(由 [snapshot: <key>] 标注引用)。

    用途:finalize 前核对 evidence 是否真实存在于数据源,或回顾被压缩的观察。
    超长内容按 max_chars 截断;完整深度分析请用 analyze_logs。
    """
    content = context_mgr.snapshot_store().get(snapshot_key)
    if content is None:
        return json.dumps({"error": f"快照不存在(可能已被淘汰),key={snapshot_key}"},
                          ensure_ascii=False)
    try:
        n = max(500, min(int(max_chars), 200000))
    except (TypeError, ValueError):
        n = 8000
    if len(content) > n:
        content = content[:n] + f"\n...(共 {len(content)} 字符,已截断)"
    return json.dumps({"content": content, "length": len(content)}, ensure_ascii=False)


@tool
def list_entities(filter_text: str = "") -> str:
    """列出当前数据源里可诊断的实体(作业/服务/节点/日志文件),返回 id 列表。

    filter_text:按名称模糊过滤;留空列出全部。开始诊断前建议先调用本工具了解范围。
    """
    try:
        entities = get_adapter().list_entities(str(filter_text or ""))
        return json.dumps({"entities": entities, "total": len(entities)}, ensure_ascii=False)
    except Exception as e:
        return json.dumps({"error": f"列出实体失败：{e}"}, ensure_ascii=False)


@tool
def query_logs(entity_id: str, keyword: str = "", limit: int = 50,
               level: str = "") -> str:
    """查询某实体的日志(只读),按关键词/级别过滤,返回日志行列表。

    entity_id:list_entities 返回的实体 id(或 conv:/trace: 前缀的精确目标);
    keyword:可选过滤关键词(默认全部行);
    level:可选级别过滤(如 ERROR / WARN / INFO,默认不过滤);
    limit:最多返回的行数(默认 50)。长结果会被系统自动压缩为快照,需要用
    analyze_logs 做深度分析。
    """
    try:
        rows = get_adapter().query_logs(
            str(entity_id), keyword=str(keyword or ""), limit=limit,
            level=str(level or ""))
        payload = {"entity": entity_id, "rows": rows, "total": len(rows)}
        if not rows:
            payload["note"] = "没有匹配日志(确认实体 id / 关键词 / 级别)"
        return json.dumps(payload, ensure_ascii=False)
    except Exception as e:
        return json.dumps({"error": f"查询日志失败：{e}"}, ensure_ascii=False)


@tool
def get_entity_detail(entity_id: str) -> str:
    """查看某实体的概要信息(状态/配置/来源等,只读),用于了解实体全貌后再查日志。"""
    try:
        detail = get_adapter().get_entity_detail(str(entity_id))
        return json.dumps({"entity": entity_id, "detail": detail}, ensure_ascii=False)
    except Exception as e:
        return json.dumps({"error": f"读取实体详情失败：{e}"}, ensure_ascii=False)


@tool
def finalize(root_cause: str, solution: str, evidence: str,
             responsibility: str, confidence: float = 0.8,
             acknowledge: bool = False) -> str:
    """宣布根因定位结论并输出结构化 RCA 报告(终止诊断)。

    必须在充分调查后调用:系统会校验 root_cause/solution/evidence 非空、
    responsibility 合法,并默认要求已做过至少 2 次数据采集(证据不足会打回,
    提示继续调查)。

    root_cause:根因描述;
    solution:缓解/修复建议;
    evidence:支撑结论的证据(应逐字引用日志/数据原文,禁止编造);
    responsibility:责任归属:user(用户侧/使用不当)或 platform(平台/服务端/基础设施);
    confidence:0~1 的置信度;
    acknowledge:自认证据不足但坚持输出时置 True(会放行并记录,不静默通过)。
    """
    rc = str(root_cause).strip()
    sol = str(solution).strip()
    ev = str(evidence).strip()
    if not rc or not sol or not ev:
        return json.dumps(
            {"error": "finalize 要求 root_cause / solution / evidence 三者均非空"},
            ensure_ascii=False)
    resp = parse_responsibility(responsibility)
    if resp is None:
        return json.dumps(
            {"error": f"responsibility 无法归一到 user/platform(收到：{responsibility!r})。"
                      "请明确判定为用户责任或平台责任。"},
            ensure_ascii=False)
    return json.dumps({
        "result": {
            "root_cause": rc,
            "solution": sol,
            "evidence": ev,
            "responsibility": resp,
            "confidence": float(confidence) if confidence is not None else 0.8,
            "acknowledge": bool(acknowledge),
        }
    }, ensure_ascii=False)