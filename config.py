"""项目配置：.env 加载 + DeepSeek API 常量。"""
import os

_ENV_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")


def load_env_file() -> None:
    """加载项目目录下的 .env 文件到 os.environ。

    规则：
      - .env 文件不存在则静默跳过；
      - 已设置的环境变量优先（不覆盖 os.environ 已有值）；
      - .env 中未设置的值作为兜底写入 os.environ。
      解析格式：每行 KEY=VALUE（# 开头为注释，值可带引号）。
    """
    if not os.path.isfile(_ENV_FILE):
        return
    try:
        with open(_ENV_FILE, encoding="utf-8") as f:
            for raw in f:
                line = raw.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, value = line.split("=", 1)
                key = key.strip()
                value = value.strip()
                # 去掉值两端的成对引号
                if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
                    value = value[1:-1]
                # 已存在的环境变量优先，不覆盖
                if key and key not in os.environ:
                    os.environ[key] = value
    except OSError:
        pass


load_env_file()

DEEPSEEK_API_KEY = os.environ.get("DEEPSEEK_API_KEY")
DEEPSEEK_MODEL = os.environ.get("DEEPSEEK_MODEL", "deepseek-v4-flash")
BASE_URL = "https://api.deepseek.com"
CHAT_URL = f"{BASE_URL}/chat/completions"

# 注意：thinking 思考模式(deepseek-v4-pro 的推理模式)不保证支持函数调用，
# 做 Agent Loop 用默认的非思考模式即可，需要工具调用时不要开 thinking。
