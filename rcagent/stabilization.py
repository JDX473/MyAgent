"""轻量 JSON 修复:把 LLM 生成的、带瑕疵的 JSON 恢复为可解析的 dict。

对应 RCAgent(arXiv:2310.16340) 论文里 JsonRegen 思想的轻量版:
纯标准库、不借助模型重生成,做梯度式的"从简单到麻烦"的修复尝试:

  1. 直接 json.loads;
  2. 剥掉代码围栏(```json ... ``` 或 ```...```);
  3. 花括号配对提取 —— 扫描所有平衡的 {...} 子串(解决"前后有多余文字/JSON 嵌套在
     普通文本里"的情况);
  4. 单引号归一 —— 把 '{...}' 里键/值的单引号换成双引号;
  5. 清理明显的多余转义。

仍失败返回 None,由调用方决定兜底(返回参数不匹配的报错,或让模型重生成)。
"""
import json
import re


def _try_parse(s: str) -> object | None:
    try:
        return json.loads(s)
    except (json.JSONDecodeError, ValueError, TypeError):
        return None


def _balanced_json_substrings(s: str) -> list[str]:
    """扫描所有从 '{' 开始、括号平衡的 JSON 候选子串,按长度降序去重。"""
    found: list[str] = []
    seen: set[str] = set()
    i = 0
    n = len(s)
    while i < n:
        if s[i] != "{":
            i += 1
            continue
        depth = 0
        in_str = False
        esc = False
        end = -1
        for j in range(i, n):
            c = s[j]
            if in_str:
                if esc:
                    esc = False
                elif c == "\\":
                    esc = True
                elif c == '"':
                    in_str = False
                continue
            if c == '"':
                in_str = True
            elif c == "{":
                depth += 1
            elif c == "}":
                depth -= 1
                if depth == 0:
                    end = j
                    break
        if end >= 0:
            cand = s[i:end + 1]
            if cand not in seen:
                seen.add(cand)
                found.append(cand)
            i = end
        i += 1
    found.sort(key=len, reverse=True)
    return found


def _dequotes(s: str) -> str:
    """把单引号包裹的键/值替换为双引号(粗粒度,仅用于倒数第二道尝试)。"""
    s = re.sub(r"(?<=[\{\[,:])\s*'", '"', s)
    s = re.sub(r"'\s*(?=[:\},\]])", '"', s)
    return s


def _clean_escapes(s: str) -> str:
    r"""清理常见的多余转义(如 \' -> '、\{ -> {)。"""
    s = s.replace("\\'", "'")
    s = re.sub(r'\\(\{|\}|\[|\])', r'\1', s)
    return s


def repair_json(raw: str) -> dict | None:
    """尽力把原始文本修复成可解析的 dict;失败返回 None。"""
    if not isinstance(raw, str):
        return None
    raw = raw.strip()

    # 1) 直接解析
    v = _try_parse(raw)
    if isinstance(v, dict):
        return v

    # 2) 剥代码围栏
    fenced = re.sub(r"```[a-z]*\s*|\s*```", "", raw, flags=re.IGNORECASE).strip()
    if fenced and fenced != raw:
        v = _try_parse(fenced)
        if isinstance(v, dict):
            return v

    # 3) 花括号配对提取(解决前后多余文字)
    for cand in _balanced_json_substrings(raw):
        v = _try_parse(cand)
        if isinstance(v, dict):
            return v

    # 4) 单引号归一
    v = _try_parse(_dequotes(raw))
    if isinstance(v, dict):
        return v

    # 5) 清理多余转义后再试一次配对提取
    cleaned = _clean_escapes(raw)
    if cleaned != raw:
        for cand in _balanced_json_substrings(cleaned):
            v = _try_parse(cand)
            if isinstance(v, dict):
                return v

    return None