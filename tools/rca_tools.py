"""RCA 工具：直连 IM 项目的日志搜索接口。"""
import json
import urllib.error
import urllib.request

from config import IM_LOGSEARCH_SEARCH_URL
from core.tools import tool

_DEFAULT_LIMIT = 20
_MAX_LIMIT = 50
_TIMEOUT = 30


def _clean_text(value: str | int | None) -> str:
    if value is None:
        return ""
    return str(value).strip()


@tool
def search_logs(
    keyword: str = "",
    time_from: str = "",
    time_to: str = "",
    level: str = "",
    regex: str = "",
    trace_id: str = "",
    conv: str = "",
    limit: int = _DEFAULT_LIMIT,
) -> str:
    """查询 IM 线上日志搜索服务，返回匹配日志与聚合结果。

    直接调用 im-logsearch 的 POST /api/v1/logs/search。
    至少提供一个查询条件；通常用 keyword 配合 time_from/time_to 缩小范围，
    也可直接按 trace_id、conv、level、regex 查询。
    """
    payload: dict[str, object] = {}

    keyword = _clean_text(keyword)
    time_from = _clean_text(time_from)
    time_to = _clean_text(time_to)
    level = _clean_text(level).upper()
    regex = _clean_text(regex)
    trace_id = _clean_text(trace_id)
    conv = _clean_text(conv)

    if keyword:
        payload["keyword"] = keyword
    if time_from:
        payload["timeFrom"] = time_from
    if time_to:
        payload["timeTo"] = time_to
    if level:
        payload["level"] = level
    if regex:
        payload["regex"] = regex
    if trace_id:
        payload["traceId"] = trace_id
    if conv:
        payload["conv"] = conv

    if not payload:
        return json.dumps(
            {
                "error": "search_logs 至少需要一个查询条件（keyword/time_from/time_to/level/regex/trace_id/conv）"
            },
            ensure_ascii=False,
        )

    try:
        limit_int = int(limit)
    except (TypeError, ValueError):
        return json.dumps({"error": "limit 必须是整数"}, ensure_ascii=False)
    payload["limit"] = max(1, min(limit_int, _MAX_LIMIT))

    request = urllib.request.Request(
        IM_LOGSEARCH_SEARCH_URL,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
        method="POST",
    )

    try:
        with urllib.request.urlopen(request, timeout=_TIMEOUT) as response:
            data = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", errors="replace")
        return json.dumps({"error": f"日志搜索 API 返回 {e.code}: {detail}"}, ensure_ascii=False)
    except (urllib.error.URLError, OSError) as e:
        return json.dumps({"error": f"日志搜索请求失败：{e}"}, ensure_ascii=False)
    except ValueError as e:
        return json.dumps({"error": f"日志搜索服务返回非 JSON 内容：{e}"}, ensure_ascii=False)

    hits = data.get("hits")
    if not isinstance(hits, list):
        hits = []

    result = {
        "request": payload,
        "total": data.get("total", 0),
        "took_millis": data.get("took_millis"),
        "levels": data.get("levels"),
        "hits": hits[: payload["limit"]],
    }
    if not result["hits"]:
        result["message"] = "没有找到匹配日志。"
    return json.dumps(result, ensure_ascii=False)
