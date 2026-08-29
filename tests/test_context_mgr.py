"""OBSK 快照机制(rcagent/context_mgr.py)的单元测试。

覆盖:
  1. SnapshotStore:put/get 往返、按内容去重、LRU 淘汰、超长拒绝
  2. auto_snapshot 钩子:长结果被压缩为 head+快照键;短结果不动;
     非 RCA 会话不压缩(会话隔离)
"""
import unittest

from core.hooks import HookContext
from rcagent.context_mgr import SnapshotStore, auto_snapshot, reset, snapshot_store


class TestSnapshotStore(unittest.TestCase):

    def setUp(self):
        reset()

    def test_put_get_roundtrip(self):
        store = snapshot_store()
        key = store.put("hello world")
        self.assertTrue(key)
        self.assertIn("snap-", key)
        self.assertEqual(store.get(key), "hello world")

    def test_get_missing_returns_none(self):
        self.assertIsNone(snapshot_store().get("snap-9999"))

    def test_dedupe_same_content_same_key(self):
        store = snapshot_store()
        k1 = store.put("abc" * 100)
        k2 = store.put("abc" * 100)
        self.assertEqual(k1, k2)

    def test_empty_content_rejected(self):
        self.assertIsNone(snapshot_store().put(""))

    def test_oversize_rejected(self):
        store = SnapshotStore(entry_max=10)
        self.assertIsNone(store.put("x" * 11))

    def test_lru_eviction(self):
        store = SnapshotStore(max_entries=2)
        k1 = store.put("one")
        k2 = store.put("two")
        k3 = store.put("three")
        self.assertIsNone(store.get(k1))  # 最早的被淘汰
        self.assertEqual(store.get(k2), "two")
        self.assertEqual(store.get(k3), "three")

    def test_touch_reorders_lru(self):
        store = SnapshotStore(max_entries=2)
        k1 = store.put("one")
        k2 = store.put("two")
        store.get(k1)          # 触碰 k1 → 它变最新
        k3 = store.put("three")
        self.assertIsNone(store.get(k2))  # k2 被淘汰
        self.assertEqual(store.get(k1), "one")


class TestAutoSnapshot(unittest.TestCase):

    def setUp(self):
        reset()

    def _ctx(self, tool: str, result: str, name: str = "RCA") -> HookContext:
        ctx = HookContext()
        ctx.name = name
        ctx.tool_name = tool
        ctx.result = result
        return ctx

    def test_long_result_compressed(self):
        long_result = "line_" + "x" * 3000
        ctx = self._ctx("bash", long_result)
        auto_snapshot(ctx)
        self.assertLess(len(ctx.result), 2000)
        self.assertIn("[snapshot:", ctx.result)
        self.assertIn("get_snapshot", ctx.result)
        # 全文仍在 store 里,可经 store 取回
        import re
        key = re.search(r"snap-\d+", ctx.result).group(0)
        self.assertEqual(snapshot_store().get(key), long_result)

    def test_short_result_untouched(self):
        ctx = self._ctx("bash", "short output")
        auto_snapshot(ctx)
        self.assertEqual(ctx.result, "short output")

    def test_skipped_tools_not_compressed(self):
        # get_snapshot / finalize 的结果本身不该再被压缩
        ctx = self._ctx("finalize", "x" * 3000)
        auto_snapshot(ctx)
        self.assertEqual(ctx.result, "x" * 3000)

    def test_non_rca_session_not_compressed(self):
        # 钩子只对 RCA 会话生效,不污染通用会话
        ctx = self._ctx("bash", "y" * 3000, name="Agent")
        auto_snapshot(ctx)
        self.assertEqual(ctx.result, "y" * 3000)


if __name__ == "__main__":
    unittest.main()