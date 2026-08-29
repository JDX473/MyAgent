"""RCA 工具(rcagent/rca_tools.py):责任归一、finalize 报告、信息采集。

用 demo 适配器(临时目录)驱动 query_logs / list_entities 的只读行为。
"""
import json
import os
import tempfile
import unittest
from unittest import mock

from core.tools import run_tool
from rcagent.adapters import get_adapter, reset_adapter
from rcagent.rca_tools import parse_responsibility


def _finalize(**kwargs):
    """调用 finalize 工具并解两层 JSON(外层 {"result":...} + 工具内层)。"""
    outer = json.loads(run_tool("finalize", json.dumps(kwargs, ensure_ascii=False)))
    inner = json.loads(outer["result"]) if "result" in outer else outer
    return inner


class TestParseResponsibility(unittest.TestCase):

    def test_known_aliases(self):
        self.assertEqual(parse_responsibility("user"), "user")
        self.assertEqual(parse_responsibility("用户"), "user")
        self.assertEqual(parse_responsibility("平台"), "platform")
        self.assertEqual(parse_responsibility("platform"), "platform")
        self.assertEqual(parse_responsibility("服务端"), "platform")

    def test_unknown_returns_none(self):
        self.assertIsNone(parse_responsibility("说不清楚"))
        self.assertIsNone(parse_responsibility(123))
        self.assertIsNone(parse_responsibility(""))


class TestFinalizeTool(unittest.TestCase):

    _base = dict(
        root_cause="ES 连接超时", solution="联系 ES 团队检查集群",
        evidence="SocketTimeoutException 出现在日志原文", responsibility="平台",
    )

    def test_success(self):
        r = _finalize(**self._base, confidence=0.8)
        self.assertEqual(r["result"]["root_cause"], self._base["root_cause"])
        self.assertEqual(r["result"]["responsibility"], "platform")
        self.assertEqual(r["result"]["confidence"], 0.8)

    def test_empty_fields_rejected(self):
        for field in ("root_cause", "solution", "evidence"):
            bad = dict(self._base)
            bad[field] = "   "
            r = _finalize(**bad)
            self.assertIn("error", r)

    def test_unknown_responsibility_rejected(self):
        bad = dict(self._base, responsibility="说不清")
        r = _finalize(**bad)
        self.assertIn("error", r)
        self.assertIn("user/platform", r["error"])

    def test_acknowledge_flag_preserved(self):
        r = _finalize(**self._base, acknowledge=True)
        self.assertTrue(r["result"]["acknowledge"])


class TestSnapshotTool(unittest.TestCase):
    """get_snapshot 取回被压缩的全文;缺失键报错。"""

    def test_get_snapshot_roundtrip(self):
        from rcagent import context_mgr
        context_mgr.reset()
        key = context_mgr.snapshot_store().put("FULL CONTENT " * 50)
        outer = json.loads(run_tool("get_snapshot", json.dumps({"snapshot_key": key})))
        inner = json.loads(outer["result"])
        self.assertIn("content", inner)
        self.assertIn("FULL CONTENT", inner["content"])

    def test_get_snapshot_missing(self):
        outer = json.loads(run_tool("get_snapshot", json.dumps({"snapshot_key": "snap-9999"})))
        inner = json.loads(outer["result"])
        self.assertIn("error", inner)


class TestCollectionToolsDemo(unittest.TestCase):
    """信息采集工具走 demo 适配器;只读、返回结构化结果。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._log = os.path.join(self._tmp.name, "job-demo.log")
        with open(self._log, "w", encoding="utf-8") as f:
            f.write("INFO start ok\nERROR java.lang.OutOfMemoryError: Java heap space\n"
                    "WARN retrying\nFATAL job failed\n")

    def tearDown(self):
        self._tmp.cleanup()
        reset_adapter()

    def _list_entities(self):
        with mock.patch.dict(os.environ, {"RCA_DEMO_DATA_DIR": self._tmp.name}):
            reset_adapter()
            outer = json.loads(run_tool("list_entities", json.dumps({"filter_text": ""})))
            return json.loads(outer["result"])

    def test_list_entities(self):
        r = self._list_entities()
        self.assertIn("job-demo.log", r["entities"])

    def test_query_logs_filter(self):
        with mock.patch.dict(os.environ, {"RCA_DEMO_DATA_DIR": self._tmp.name}):
            reset_adapter()
            outer = json.loads(run_tool(
                "query_logs",
                json.dumps({"entity_id": "job-demo.log", "keyword": "OutOfMemory"})))
            r = json.loads(outer["result"])
        self.assertEqual(r["total"], 1)
        self.assertIn("OutOfMemoryError", r["rows"][0])

    def test_query_logs_unknown_entity(self):
        with mock.patch.dict(os.environ, {"RCA_DEMO_DATA_DIR": self._tmp.name}):
            reset_adapter()
            outer = json.loads(run_tool("query_logs", json.dumps({"entity_id": "nope.log"})))
            r = json.loads(outer["result"])
        self.assertEqual(r["total"], 0)

    def test_get_entity_detail(self):
        with mock.patch.dict(os.environ, {"RCA_DEMO_DATA_DIR": self._tmp.name}):
            reset_adapter()
            outer = json.loads(run_tool(
                "get_entity_detail", json.dumps({"entity_id": "job-demo.log"})))
            r = json.loads(outer["result"])
        self.assertIn("行数", r["detail"])


class TestSubagentExcludesRcaDepth(unittest.TestCase):
    """subAgent 内层不应暴露 analyze_logs / finalize,封死递归与结构化出口。"""

    def test_subagent_allowed_excludes_rca_expert_tools(self):
        from tools.subagent_tool import _subagent_allowed
        allowed = _subagent_allowed()
        self.assertNotIn("analyze_logs", allowed)
        self.assertNotIn("finalize", allowed)


if __name__ == "__main__":
    unittest.main()