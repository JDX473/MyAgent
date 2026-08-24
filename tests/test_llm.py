"""LLM 通信封装（chat_completion）的单元测试。

用 mock 替换 urllib，不发起真实网络请求。
"""
import json
import unittest
from unittest import mock

from core import llm


class _FakeResponse:
    def __init__(self, body: bytes):
        self._body = body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self):
        return self._body


class TestChatCompletion(unittest.TestCase):

    def test_builds_request_and_parses_response(self):
        fake = _FakeResponse(json.dumps({"choices": [{"message": {"content": "hi"}}]}).encode())
        with mock.patch("core.llm.urllib.request.urlopen", return_value=fake) as m:
            result = llm.chat_completion(
                [{"role": "user", "content": "hello"}],
                tools=[{"type": "function"}],
            )
        # 返回解析后的 JSON
        self.assertEqual(result["choices"][0]["message"]["content"], "hi")
        # 校验请求构造
        req = m.call_args[0][0]
        self.assertEqual(req.method, "POST")
        self.assertEqual(req.full_url, llm.CHAT_URL)
        self.assertEqual(req.headers["Content-type"], "application/json")
        self.assertTrue(req.headers["Authorization"].startswith("Bearer "))
        # payload 含 model / messages / tools
        payload = json.loads(req.data)
        self.assertEqual(payload["model"], llm.DEEPSEEK_MODEL)
        self.assertEqual(payload["messages"], [{"role": "user", "content": "hello"}])
        self.assertEqual(payload["tools"], [{"type": "function"}])

    def test_no_tools_when_none(self):
        fake = _FakeResponse(b'{"choices": [{}]}')
        with mock.patch("core.llm.urllib.request.urlopen", return_value=fake) as m:
            llm.chat_completion([{"role": "user", "content": "hi"}])
        payload = json.loads(m.call_args[0][0].data)
        self.assertNotIn("tools", payload)

    def test_http_error_raises_runtime_error(self):
        err = mock.MagicMock()
        err.code = 401
        err.read.return_value = b'{"error": "unauthorized"}'
        with mock.patch("core.llm.urllib.request.urlopen", side_effect=llm.urllib.error.HTTPError(
                "url", 401, "Unauthorized", {}, None)) as m:
            m.side_effect.read = lambda: err.read()
            with self.assertRaises(RuntimeError) as ctx:
                llm.chat_completion([{"role": "user", "content": "hi"}])
        self.assertIn("401", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
