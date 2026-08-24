"""websearch 工具：让 Agent 具备联网搜索能力。

基于博查（Bocha）Web Search API，返回"标题 + URL + 摘要"的结构化结果，
供模型阅读后回答。调用前需在 .env 中配置 BOCHA_API_KEY。
"""
import json
import urllib.error
import urllib.request

from config import BOCHA_API_KEY, BOCHA_WEB_SEARCH_URL
from core.tools import tool

# 单次最多返回的搜索结果条数
_MAX_RESULTS = 10


@tool
def websearch(query: str, count: int = 5) -> str:
    """在互联网上搜索，返回前几条结果的标题、链接与摘要。

    用于查询需要实时或外部信息的问题（新闻、百科、资料等）。
    参数 count 为返回结果条数，1-10。
    """
    if not BOCHA_API_KEY:
        return json.dumps({"error": "未配置 BOCHA_API_KEY，无法联网搜索。"}, ensure_ascii=False)

    # 限制条数，避免返回过多结果
    n = max(1, min(count, _MAX_RESULTS))

    payload = {
        "query": query,
        "summary": True,   # 请求长文本摘要，便于模型理解
        "count": n,
        "freshness": "noLimit",
    }
    request = urllib.request.Request(
        BOCHA_WEB_SEARCH_URL,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {BOCHA_API_KEY}",
        },
        method="POST",
    )

    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            data = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", errors="replace")
        return json.dumps({"error": f"搜索 API 返回 {e.code}: {detail}"}, ensure_ascii=False)
    except (urllib.error.URLError, OSError) as e:
        return json.dumps({"error": f"搜索请求失败：{e}"}, ensure_ascii=False)

    # 响应格式兼容 Bing Search：结果在 data.webPages.value
    pages = (data.get("data") or {}).get("webPages") or {}
    results = pages.get("value") or []

    if not results:
        return json.dumps({"results": [], "message": "没有找到相关结果。"}, ensure_ascii=False)

    # 精简为"标题 + URL + 摘要"，便于模型阅读
    simplified = []
    for r in results[:n]:
        simplified.append({
            "title": r.get("name"),
            "url": r.get("url"),
            "snippet": (r.get("summary") or r.get("snippet") or "").strip(),
        })
    return json.dumps({"results": simplified, "total": len(simplified)}, ensure_ascii=False)
