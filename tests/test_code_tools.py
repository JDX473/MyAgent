"""代码检查工具 search_code/search_symbol/read_code 的单元测试。"""
import json
import os
import shutil
import unittest
from unittest import mock

import tools.file_tools as ft
from tools import code_tools as ct


class CodeToolsSandbox(unittest.TestCase):

    def setUp(self):
        self.orig_workdir = ft._WORK_DIR
        self.sandbox = os.path.join(
            self.orig_workdir,
            ".agent_test_sandbox",
            self.__class__.__name__,
            self._testMethodName,
        )
        shutil.rmtree(self.sandbox, ignore_errors=True)
        os.makedirs(self.sandbox, exist_ok=True)
        ft._WORK_DIR = self.sandbox

    def tearDown(self):
        ft._WORK_DIR = self.orig_workdir
        shutil.rmtree(self.sandbox, ignore_errors=True)

    def _write(self, path: str, content: str) -> str:
        full = os.path.join(self.sandbox, *path.split("/"))
        os.makedirs(os.path.dirname(full), exist_ok=True)
        with open(full, "w", encoding="utf-8") as f:
            f.write(content)
        return full


class TestSearchCode(CodeToolsSandbox):

    def test_finds_literal_matches_sorted_and_excludes_generated_paths(self):
        self._write("service/handler.py", "def handle():\n    return 'timeout'\n")
        self._write("app.py", "TIMEOUT_MESSAGE = 'timeout'\n")
        self._write("__pycache__/ignored.py", "timeout\n")
        self._write("generated/auto.py", "timeout\n")

        result = json.loads(ct.search_code("timeout", limit=10, context=1))

        self.assertEqual(result["total"], 2)
        self.assertEqual([hit["file"] for hit in result["hits"]], ["app.py", "service/handler.py"])
        self.assertEqual(result["hits"][0]["line"], 1)
        self.assertEqual(result["hits"][1]["context"][0]["line"], 1)

    def test_respects_subtree_restriction(self):
        self._write("service/handler.py", "needle = True\n")
        self._write("other.py", "needle = False\n")

        result = json.loads(ct.search_code("needle", path="service", limit=10))

        self.assertEqual(result["total"], 1)
        self.assertEqual(result["hits"][0]["file"], "service/handler.py")

    def test_finds_regex_matches(self):
        self._write("service/errors.py", "ERROR_CODE = 'ERR42'\n")

        result = json.loads(ct.search_code(r"ERR\d+", regex=True))

        self.assertEqual(result["total"], 1)
        self.assertEqual(result["hits"][0]["file"], "service/errors.py")

    def test_falls_back_to_python_scanner_when_rg_is_missing(self):
        self._write("app.py", "needle = True\n")

        with mock.patch("tools.code_tools.shutil.which", return_value=None):
            result = json.loads(ct.search_code("needle"))

        self.assertEqual(result["backend"], "python")
        self.assertEqual(result["total"], 1)


class TestSearchSymbol(CodeToolsSandbox):

    def test_finds_python_definitions_references_and_skips_parse_errors(self):
        self._write(
            "service/handler.py",
            "\n".join([
                "ERROR_TEXT = 'Database timeout'",
                "",
                "class Handler:",
                "    def process(self):",
                "        return helper(ERROR_TEXT)",
                "",
                "def helper(value):",
                "    return value",
                "",
                "def wrapper():",
                "    def helper():",
                "        return 'nested'",
                "    return helper()",
                "",
            ]),
        )
        self._write("bad.py", "def broken(:\n")

        result = json.loads(ct.search_symbol("ERROR_TEXT", limit=10))

        self.assertEqual(result["hits"][0]["role"], "definition")
        self.assertEqual(result["hits"][0]["kind"], "constant")
        self.assertEqual(result["hits"][0]["qualified_name"], "ERROR_TEXT")
        self.assertTrue(any(hit["role"] == "reference" for hit in result["hits"]))
        self.assertEqual(result["skipped"][0]["file"], "bad.py")

        function_result = json.loads(ct.search_symbol("process", kind="function"))
        self.assertEqual(function_result["hits"][0]["qualified_name"], "Handler.process")

        helper_result = json.loads(ct.search_symbol("helper", limit=10))
        definitions = [hit["qualified_name"] for hit in helper_result["hits"] if hit["role"] == "definition"]
        self.assertEqual(definitions, ["helper", "wrapper.helper"])


class TestReadCode(CodeToolsSandbox):

    def test_reads_exact_numbered_window(self):
        self._write("app.py", "\n".join(f"line{i}" for i in range(1, 6)) + "\n")

        result = json.loads(ct.read_code("app.py", 2, 4))

        self.assertEqual(result["content"], "2 | line2\n3 | line3\n4 | line4")
        self.assertEqual(result["start_line"], 2)
        self.assertEqual(result["end_line"], 4)

    def test_rejects_invalid_and_out_of_range_requests(self):
        self._write("app.py", "one\n")

        self.assertIn("error", json.loads(ct.read_code("app.py", 0, 1)))
        self.assertIn("error", json.loads(ct.read_code("app.py", 2, 2)))
        self.assertIn("error", json.loads(ct.read_code("app.py", 1, 2)))

    def test_clamps_oversized_ranges(self):
        self._write("app.py", "\n".join(f"line{i}" for i in range(1, 251)) + "\n")

        result = json.loads(ct.read_code("app.py", 1, 250))

        self.assertTrue(result["truncated"])
        self.assertEqual(result["end_line"], 200)
        self.assertIn("200 | line200", result["content"])


class TestRcaInspectionFlow(CodeToolsSandbox):

    def test_symbol_to_code_slice_to_error_string_search(self):
        self._write(
            "service/db.py",
            "\n".join([
                "ERROR_TEXT = 'Database timeout while loading profile'",
                "",
                "def fetch_profile(user_id):",
                "    raise RuntimeError(ERROR_TEXT)",
                "",
            ]),
        )

        symbol = json.loads(ct.search_symbol("fetch_profile"))
        line = symbol["hits"][0]["line"]
        snippet = json.loads(ct.read_code("service/db.py", line, line + 1))
        error_search = json.loads(ct.search_code("Database timeout", path="service"))

        self.assertIn("def fetch_profile", snippet["content"])
        self.assertEqual(error_search["hits"][0]["file"], "service/db.py")


if __name__ == "__main__":
    unittest.main()
