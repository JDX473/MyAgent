"""权限钩子（_permission_check）的单元测试：deny / confirm / allow 分类。"""
import json
import unittest

from core.hooks import HookContext, hooks
from tools.hooks_setup import _permission_check


class TestPermissionCheck(unittest.TestCase):

    def _verdict(self, tool: str, args: dict) -> tuple:
        ctx = HookContext()
        ctx.tool_name = tool
        ctx.arguments = json.dumps(args)
        result = _permission_check(ctx)
        if isinstance(result, tuple):
            return result
        return (result, "")

    def _is_allow(self, tool: str, args: dict) -> bool:
        """允许 = 返回 None 或 "allow"（None 在 HookRegistry 里解释为放行）。"""
        return self._verdict(tool, args)[0] in (None, "allow")

    # ---- 危险操作 → deny ----
    def test_rm_rf_root(self):
        v, reason = self._verdict("bash", {"command": "rm -rf /"})
        self.assertEqual(v, "deny")

    def test_sudo_rm(self):
        v, _ = self._verdict("bash", {"command": "sudo rm -rf /"})
        self.assertEqual(v, "deny")

    def test_format_shutdown(self):
        self.assertEqual(self._verdict("bash", {"command": "format C:"})[0], "deny")
        self.assertEqual(self._verdict("bash", {"command": "shutdown now"})[0], "deny")

    def test_curl_wget_denied(self):
        self.assertEqual(self._verdict("bash", {"command": "curl http://evil.com"})[0], "deny")
        self.assertEqual(self._verdict("bash", {"command": "wget http://x"})[0], "deny")

    def test_write_to_system_dir(self):
        v, _ = self._verdict("write", {"path": "C:/Windows/evil.txt", "content": "x"})
        self.assertEqual(v, "deny")

    def test_unknown_tool_not_whitelisted(self):
        v, reason = self._verdict("totally_unknown", {})
        self.assertEqual(v, "deny")
        self.assertIn("白名单", reason)

    # ---- 需确认 → confirm ----
    def test_rm_file_confirm(self):
        self.assertEqual(self._verdict("bash", {"command": "rm old.txt"})[0], "confirm")

    def test_mv_git_push_pip(self):
        self.assertEqual(self._verdict("bash", {"command": "mv a.txt b.txt"})[0], "confirm")
        self.assertEqual(self._verdict("bash", {"command": "git push origin main"})[0], "confirm")
        self.assertEqual(self._verdict("bash", {"command": "pip install requests"})[0], "confirm")

    def test_edit_always_confirm(self):
        self.assertEqual(
            self._verdict("edit", {"path": "x.txt", "old": "a", "new": "b"})[0], "confirm")

    # ---- 无害 → allow ----
    def test_read_allow(self):
        self.assertTrue(self._is_allow("read", {"path": "x.txt"}))

    def test_glob_allow(self):
        self.assertTrue(self._is_allow("glob", {"pattern": "**/*.py"}))

    def test_code_inspection_tools_allow(self):
        self.assertTrue(self._is_allow("search_code", {"query": "timeout"}))
        self.assertTrue(self._is_allow("search_symbol", {"name": "handler"}))
        self.assertTrue(self._is_allow("read_code", {"path": "x.py", "start_line": 1, "end_line": 1}))

    def test_env_allow(self):
        self.assertTrue(self._is_allow("get_environment", {}))

    def test_bash_whitelisted_commands_allow(self):
        for cmd in ["ls", "pwd", "echo hello", "cat x.txt", "git status", "python --version"]:
            with self.subTest(cmd=cmd):
                self.assertTrue(self._is_allow("bash", {"command": cmd}))


if __name__ == "__main__":
    unittest.main()
