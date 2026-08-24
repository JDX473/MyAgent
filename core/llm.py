"""DeepSeek 原生 REST API 调用封装（不依赖 OpenAI SDK）。"""
import json
import urllib.error
import urllib.request

from config import CHAT_URL, DEEPSEEK_API_KEY, DEEPSEEK_MODEL


def chat_completion(messages: list[dict], tools: list[dict] | None = None) -> dict:
    """调用 DeepSeek 原生 /chat/completions 端点，返回解析后的 JSON。"""
    payload: dict = {
        "model": DEEPSEEK_MODEL,
        "messages": messages,
        "stream": False,
    }
    if tools:
        payload["tools"] = tools

    request = urllib.request.Request(
        CHAT_URL,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {DEEPSEEK_API_KEY}",
        },
        method="POST",
    )

    try:
        with urllib.request.urlopen(request) as response:
            body = response.read().decode("utf-8")
            return json.loads(body)
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"DeepSeek API 返回 {e.code}: {detail}") from e
