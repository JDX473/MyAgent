"""im-logsearch 日志平台适配器(rcagent/adapters/im_logsearch.py)的单元测试。

覆盖:
  1. 实体分类:默认 logger→regex、conv:/trace: 前缀精确过滤
  2. payload 构造:level/keyword/limit/timeFrom 透传 + 级别大写
  3. 结果:返回 raw 原文行(list[str]);空结果、实体概要
  4. 真实 HTTP 往返:stub server 验证 base_url、POST body、行提取
  5. 连接失败 → 可读 RuntimeError(不静默)
"""
import json
import socket
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from rcagent.adapters.im_logsearch import ImLogsearchDataSource, _classify_entity


def _stub_hits():
    raw = ("2026-08-21T12:35:48.578+08:00 ERROR 47216 --- [im-chat] "
           "[   scheduling-1] c.q.im.chat.service.OutboxService : outbox scan error")
    return [{
        "ts": "2026-08-21T12:35:48.578+08:00", "level": "ERROR",
        "logger": "c.q.im.chat.service.OutboxService", "thread": "scheduling-1",
        "trace_id": "", "conv": "", "msg": "outbox scan error", "raw": raw,
    }]


class _FakeSearch:
    """记录 payload 并返回预置响应的假 transport。"""

    def __init__(self, resp):
        self.payloads = []
        self.resp = resp

    def __call__(self, payload):
        self.payloads.append(payload)
        return self.resp


def _adapter(resp):
    """构造注入假 transport 的适配器,返回 (adapter, fake)。"""
    fake = _FakeSearch(resp)
    return ImLogsearchDataSource(search_fn=fake), fake


class TestClassifyEntity(unittest.TestCase):

    def test_logger_becomes_regex(self):
        p = _classify_entity("c.q.im.chat.service.OutboxService")
        self.assertRegex(p["regex"], r"OutboxService")
        self.assertIn("\\", p["regex"])  # 点号被转义

    def test_conv_prefix(self):
        self.assertEqual(_classify_entity("conv:u_a#u_b"), {"conv": "u_a#u_b"})

    def test_trace_prefix(self):
        self.assertEqual(_classify_entity("trace:2087871653865947140"),
                         {"traceId": "2087871653865947140"})


class TestQueryLogs(unittest.TestCase):

    def test_payload_built_and_rows_extracted(self):
        ad, fake = _adapter({"total": 1, "hits": _stub_hits()})
        rows = ad.query_logs("c.q.im.chat.service.OutboxService",
                             keyword="scan", limit=10, level="ERROR")
        p = fake.payloads[0]
        self.assertEqual(p["level"], "ERROR")
        self.assertEqual(p["keyword"], "scan")
        self.assertEqual(p["limit"], 10)
        self.assertIn("timeFrom", p)
        self.assertIn("OutboxService", p["regex"])
        self.assertEqual(len(rows), 1)
        self.assertIn("outbox scan error", rows[0])

    def test_conv_entity_uses_conv_filter(self):
        ad, fake = _adapter({"total": 0, "hits": []})
        ad.query_logs("conv:u_a#u_b")
        self.assertEqual(fake.payloads[0]["conv"], "u_a#u_b")

    def test_empty_hits_returns_empty(self):
        ad, _ = _adapter({"total": 0, "hits": []})
        self.assertEqual(ad.query_logs("nothing-here"), [])

    def test_level_lowercase_normalized(self):
        ad, fake = _adapter({"total": 0, "hits": []})
        ad.query_logs("X", level="error")
        self.assertEqual(fake.payloads[0]["level"], "ERROR")


class TestListEntities(unittest.TestCase):

    def test_loggers_from_recent_hits(self):
        ad, fake = _adapter({"total": 2, "hits": _stub_hits() * 2})
        names = ad.list_entities()
        self.assertIn("OutboxService", names)

    def test_empty_hits_fallback_to_services(self):
        ad, _ = _adapter({"total": 0, "hits": []})
        names = ad.list_entities()
        self.assertIn("im-chat", names)

    def test_filter_text(self):
        ad, _ = _adapter({"total": 2, "hits": _stub_hits() * 2})
        self.assertIn("OutboxService", ad.list_entities("outbox"))
        self.assertEqual(ad.list_entities("nonexistent-component"), [])


class TestGetEntityDetail(unittest.TestCase):

    def test_summary_contains_top_msg(self):
        ad, _ = _adapter({"total": 9, "levels": {"ERROR": 9},
                               "hits": _stub_hits() * 9})
        detail = ad.get_entity_detail("OutboxService")
        self.assertIn("9", detail)
        self.assertIn("outbox scan error", detail)

    def test_no_hits_summary(self):
        ad, _ = _adapter({"total": 0, "levels": {"ERROR": 0}, "hits": []})
        detail = ad.get_entity_detail("X")
        self.assertIn("无日志", detail)


class _StubHandler(BaseHTTPRequestHandler):
    received = []

    def do_POST(self):
        n = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(n).decode("utf-8")
        _StubHandler.received.append((self.path, body))
        resp = {"total": 1, "levels": {"ERROR": 1, "WARN": 0, "INFO": 0},
                "hits": _stub_hits()}
        resp_body = json.dumps(resp).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(resp_body)))
        self.end_headers()
        self.wfile.write(resp_body)

    def log_message(self, *args):
        pass


class TestHttpRoundTrip(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls._server = ThreadingHTTPServer(("127.0.0.1", 0), _StubHandler)
        cls._thread = threading.Thread(target=cls._server.serve_forever, daemon=True)
        cls._thread.start()
        cls.base = f"http://127.0.0.1:{cls._server.server_address[1]}"
        _StubHandler.received = []

    @classmethod
    def tearDownClass(cls):
        cls._server.shutdown()
        cls._server.server_close()

    def test_query_via_real_http(self):
        ad = ImLogsearchDataSource(base_url=self.base)
        rows = ad.query_logs("OutboxService", keyword="scan", level="ERROR")
        self.assertEqual(len(rows), 1)
        self.assertIn("outbox scan error", rows[0])
        # 确认 POST 打到了 /search 且 body 是合法 JSON 请求
        self.assertTrue(_StubHandler.received)
        path, body = _StubHandler.received[-1]
        self.assertEqual(path, "/api/v1/logs/search")
        parsed = json.loads(body)
        self.assertIn("regex", parsed)
        self.assertEqual(parsed["keyword"], "scan")

    def test_connection_refused_gives_helpful_error(self):
        # 拿一个已关闭端口的地址 → 应抛可读 RuntimeError
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
        s.close()
        ad = ImLogsearchDataSource(base_url=f"http://127.0.0.1:{port}")
        with self.assertRaises(RuntimeError) as cm:
            ad.query_logs("OutboxService")
        self.assertIn("im-logsearch", str(cm.exception))
        self.assertIn("请求失败", str(cm.exception))


if __name__ == "__main__":
    unittest.main()