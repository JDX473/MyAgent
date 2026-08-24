"""文件工具（read/write/edit/glob + _safe_path 沙箱）的单元测试。

用临时工作目录作为沙箱根，避免污染真实项目目录。
"""
import json
import os
import shutil
import tempfile
import unittest

from tools import file_tools as ft


class BaseFileToolsTest(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.orig_workdir = ft._WORK_DIR
        # 每个测试类用独立的临时目录作为沙箱
        cls.sandbox = tempfile.mkdtemp(prefix="agent_test_")
        ft._WORK_DIR = cls.sandbox

    @classmethod
    def tearDownClass(cls):
        ft._WORK_DIR = cls.orig_workdir
        shutil.rmtree(cls.sandbox, ignore_errors=True)


class TestSafePath(BaseFileToolsTest):

    def test_relative_resolved_under_workdir(self):
        p = ft._safe_path("a/b.txt")
        self.assertTrue(p.startswith(self.sandbox))

    def test_traversal_rejected(self):
        with self.assertRaises(ValueError):
            ft._safe_path("../outside.txt")

    def test_absolute_outside_rejected(self):
        with self.assertRaises(ValueError):
            ft._safe_path("C:/Windows/win.ini")

    def test_empty_rejected(self):
        with self.assertRaises(ValueError):
            ft._safe_path("")

    def test_workdir_itself_allowed(self):
        # 工作目录本身允许（glob 等需要）
        p = ft._safe_path(".")
        self.assertEqual(p, self.sandbox)


class TestWriteRead(BaseFileToolsTest):

    def test_write_then_read_roundtrip(self):
        r = json.loads(ft.write("sub/hello.txt", "hello world"))
        self.assertTrue(r["ok"])
        r2 = json.loads(ft.read("sub/hello.txt"))
        self.assertEqual(r2["content"], "hello world")

    def test_write_creates_parent_dirs(self):
        r = json.loads(ft.write("deep/nested/file.txt", "x"))
        self.assertTrue(os.path.isfile(os.path.join(self.sandbox, "deep", "nested", "file.txt")))

    def test_write_outside_rejected(self):
        r = json.loads(ft.write("../evil.txt", "x"))
        self.assertIn("error", r)


class TestEdit(BaseFileToolsTest):

    def test_edit_replaces_first_occurrence(self):
        ft.write("f.txt", "aaa bbb aaa")
        r = json.loads(ft.edit("f.txt", "aaa", "X"))
        self.assertTrue(r["ok"])
        self.assertEqual(json.loads(ft.read("f.txt"))["content"], "X bbb aaa")

    def test_edit_not_found_returns_error(self):
        ft.write("f.txt", "hello")
        r = json.loads(ft.edit("f.txt", "zzz", "X"))
        self.assertIn("error", r)
        # 文件内容不应被改动
        self.assertEqual(json.loads(ft.read("f.txt"))["content"], "hello")

    def test_edit_missing_file(self):
        r = json.loads(ft.edit("nope.txt", "a", "b"))
        self.assertIn("error", r)


class TestGlob(BaseFileToolsTest):

    def test_glob_finds_py_files(self):
        ft.write("a.py", "x")
        ft.write("b.py", "x")
        ft.write("c.txt", "x")
        r = json.loads(ft.glob("*.py"))
        self.assertEqual(r["total"], 2)
        names = [os.path.basename(m) for m in r["matches"]]
        self.assertEqual(set(names), {"a.py", "b.py"})

    def test_glob_recursive_double_star(self):
        ft.write("nested/deep/f.py", "x")
        r = json.loads(ft.glob("**/*.py"))
        self.assertGreaterEqual(r["total"], 1)
        self.assertTrue(any(m.endswith("f.py") for m in r["matches"]))

    def test_glob_traversal_rejected(self):
        r = json.loads(ft.glob("../*.py"))
        self.assertIn("error", r)

    def test_glob_empty_pattern(self):
        r = json.loads(ft.glob(""))
        self.assertIn("error", r)


if __name__ == "__main__":
    unittest.main()
