"""im-logsearch 日志查询平台适配器(QuantumLink IM 项目的内嵌 Lucene 平台)。

对接的 HTTP API(见 IM 仓库 im-logsearch 模块):
    POST {base}/api/v1/logs/search
      body: {timeFrom, timeTo, level, keyword, regex, traceId, conv, limit}
      resp: {total, took_millis, levels:{INFO,WARN,ERROR},
             hits:[{ts,level,logger,thread,trace_id,conv,msg,raw}]}

实体语义(对齐 RCA 工具的 entity_id):
  - 默认:entity_id 是组件/类名关键词(如 c.q.im.chat.service.OutboxService),
    适配器转成 raw 行上的正则过滤(.*转义名.*);list_entities 返回最近异常里
    出现过的组件名,方便定位"哪个组件在报错"。
  - 前缀精确目标:conv:<convId> → conv 精确过滤;trace:<id> → traceId 精确过滤。

平台约定(来自其 LogParser):
  - 只有带时间戳的 chat 主格式行会进索引;connect 等无时间戳格式被跳过;
  - 多行异常栈不会被索引(只收录首行);
  - 查询总带时间下限(默认最近 24h,RCA_IM_LOGSEARCH_LOOKBACK_HOURS 可调),聚焦近期异常。

只读:纯 HTTP 查询,无任何写接口调用。运行前提:im-logsearch 服务在跑
(默认 http://127.0.0.1:8083,RCA_IM_LOGSEARCH_URL 可覆盖)。
"""
import json
import os
import re
import time
import urllib.error
import urllib.request
from typing import Callable

from rcagent.adapters.base import DataSource, register_adapter

_DEFAULT_BASE = os.environ.get("RCA_IM_LOGSEARCH_URL", "http://127.0.0.1:8083")
_DEFAULT_LOOKBACK_HOURS = float(os.environ.get("RCA_IM_LOGSEARCH_LOOKBACK_HOURS", "24"))
_HTTP_TIMEOUT = 30


def _lookback_ms(hours: float = _DEFAULT_LOOKBACK_HOURS) -> int:
    return int((time.time() - hours * 3600) * 1000)


def _esc_re(s: str) -> str:
    """转义正则元字符(Lucene RegexpQuery 子集;raw 是整行单 token,可 .* 锚定)。"""
    return re.escape(s)


def _classify_entity(entity_id: str) -> dict:
    """把 entity_id 翻译成 im-logsearch 的查询参数。

    返回:带可选 conv/traceId/regex 的字典(供拼进 payload)。
    """
    e = entity_id.strip()
    if e.startswith("conv:"):
        return {"conv": e[len("conv:"):]}
    if e.startswith("trace:"):
        return {"traceId": e[len("trace:"):]}
    return {"regex": f".*{_esc_re(e)}.*"}


class ImLogsearchDataSource(DataSource):
    """对接 im-logsearch 的只读数据源。"""

    name = "im-logsearch"

    def __init__(self, base_url: str | None = None, lookback_hours: float | None = None,
                 search_fn: Callable | None = None) -> None:
        self.base = (base_url or _DEFAULT_BASE).rstrip("/")
        self.search_url = f"{self.base}/api/v1/logs/search"
        self.lookback_hours = lookback_hours if lookback_hours is not None else _DEFAULT_LOOKBACK_HOURS
        self._search_fn = search_fn  # 测试注入用;None 走真实 HTTP

    def _search(self, payload: dict) -> dict:
        if self._search_fn is not None:
            return self._search_fn(payload)
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(
            self.search_url, data=body,
            headers={"Content-Type": "application/json"}, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=_HTTP_TIMEOUT) as r:
                return json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"im-logsearch 返回 {e.code}: {detail}") from e
        except urllib.error.URLError as e:
            raise RuntimeError(
                f"im-logsearch 请求失败(服务是否在 {self.base} 运行?): {e}") from e
        except OSError as e:
            raise RuntimeError(f"im-logsearch 请求失败: {e}") from e

    def _recent_payload(self, limit: int = 200, level: str = "") -> dict:
        payload = {"timeFrom": _lookback_ms(self.lookback_hours), "limit": limit}
        if level:
            payload["level"] = level
        return payload

    def _entity_query(self, entity_id: str, limit: int, level: str,
                      keyword: str) -> dict:
        payload = self._recent_payload(limit, level=level)
        payload.update(_classify_entity(entity_id))
        if keyword:
            payload["keyword"] = keyword
        return payload

    # ------------------------------------------------------------ DataSource
    def list_entities(self, filter_text: str = "") -> list[str]:
        """返回最近窗口里出现过异常(ERROR/WARN)的组件名(logger)== 待查实体。

        无异常日志时退回已知服务名;filter_text 做子串过滤。
        """
        resp = self._search(self._recent_payload(limit=200))
        hits = resp.get("hits") or []
        loggers: list[str] = []
        seen: set[str] = set()
        for h in hits:
            lgr = (h.get("logger") or "").strip()
            # 返回简短的类名(如 OutboxService),可读且同样能反查;
            # 全限定名留在 detail 里按需展开
            key = lgr.split(".")[-1] if lgr else (h.get("msg") or "").strip()[:24]
            if key and key not in seen:
                seen.add(key)
                loggers.append(key)
        if not loggers:
            loggers = ["im-chat", "im-connect", "im-gateway", "im-logsearch"]
        if filter_text:
            f = filter_text.lower()
            loggers = [l for l in loggers if f in l.lower()]
        return loggers

    def query_logs(self, entity_id: str, keyword: str = "",
                   limit: int = 50, level: str = "") -> list[str]:
        """查实体日志,返回原文行(list[str],逐字可供证据引用)。"""
        n = max(1, min(int(limit or 50), 2000))
        payload = self._entity_query(entity_id, n, (level or "").upper(), keyword or "")
        resp = self._search(payload)
        hits = resp.get("hits") or []
        return [(h.get("raw") or "").strip() for h in hits if (h.get("raw") or "").strip()]

    def get_entity_detail(self, entity_id: str) -> str:
        """实体概要:窗口内命中数、级别分布、首末时间、top 消息模式。"""
        payload = self._entity_query(entity_id, 200, level="", keyword="")
        resp = self._search(payload)
        hits = resp.get("hits") or []
        levels = resp.get("levels") or {}
        total = resp.get("total", 0)
        if not hits:
            return (f"实体 {entity_id} 在当前窗口内无日志(命中 {total} 条;"
                    f"级别分布 {levels})。可尝试去掉级别/关键词过滤,或放大时间窗口。")
        newest = hits[0].get("ts", "")
        oldest = hits[-1].get("ts", "")
        msgs: dict[str, int] = {}
        for h in hits:
            m = (h.get("msg") or "").strip() or "(空)"
            key = m[:40] + ("…" if len(m) > 40 else "")
            msgs[key] = msgs.get(key, 0) + 1
        top = "\n  ".join(f"{n} 次 {m}" for m, n in
                          sorted(msgs.items(), key=lambda kv: -kv[1])[:5])
        return (f"实体: {entity_id}\n"
                f"窗口内命中: {total} 条(级别分布 {levels})\n"
                f"时间范围: {oldest} → {newest}\n"
                f"top 消息模式:\n  {top}")


# 导入本模块即注册为 RCA_ADAPTER=im-logsearch 的实现
register_adapter("im-logsearch", ImLogsearchDataSource)