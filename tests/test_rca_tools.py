"""search_logs 工具的单元测试：mock IM 日志搜索 API。"""
import json
import unittest
from unittest import mock

from tools import rca_tools


class _FakeResponse:
    def __init__(self, body: bytes):
        self._body = body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self):
        return self._body


class TestSearchLogs(unittest.TestCase):

    def test_builds_post_request_and_parses_response(self):
        fake = _FakeResponse(
            json.dumps({
                "total": 2,
                "took_millis": 17,
                "levels": {"ERROR": 2},
                "hits": [
                    {"timestamp": "2026-09-04T10:00:00Z", "level": "ERROR", "message": "first"},
                    {"timestamp": "2026-09-04T10:00:01Z", "level": "ERROR", "message": "second"},
                ],
            }).encode("utf-8")
        )
        with mock.patch("tools.rca_tools.urllib.request.urlopen", return_value=fake) as m:
            result = json.loads(
                rca_tools.search_logs(
                    keyword="timeout",
                    time_from="2026-09-04 10:00:00",
                    time_to="2026-09-04 10:05:00",
                    level="error",
                    limit=7,
                )
            )

        req = m.call_args[0][0]
        self.assertEqual(req.method, "POST")
        self.assertEqual(req.full_url, rca_tools.IM_LOGSEARCH_SEARCH_URL)
        payload = json.loads(req.data)
        self.assertEqual(payload["keyword"], "timeout")
        self.assertEqual(payload["timeFrom"], "2026-09-04 10:00:00")
        self.assertEqual(payload["timeTo"], "2026-09-04 10:05:00")
        self.assertEqual(payload["level"], "ERROR")
        self.assertEqual(payload["limit"], 7)
        self.assertEqual(result["total"], 2)
        self.assertEqual(result["took_millis"], 17)
        self.assertEqual(len(result["hits"]), 2)
        self.assertEqual(result["hits"][0]["message"], "first")

    def test_rejects_empty_filters(self):
        result = json.loads(rca_tools.search_logs(limit=5))
        self.assertIn("error", result)
        self.assertIn("至少需要一个查询条件", result["error"])


if __name__ == "__main__":
    unittest.main()
