"""Base64 编码 / 解码转换器。"""

import base64

from core.decorator import converter


@converter(
    method="GET",
    category="encoding",
    example_input={"text": "hello", "url_safe": False},
    example_output="aGVsbG8=",
)
def base64_encode(text: str, url_safe: bool = False) -> str:
    """把文本进行 Base64 编码。"""
    raw = text.encode("utf-8")
    encoded = base64.urlsafe_b64encode(raw) if url_safe else base64.b64encode(raw)
    out = encoded.decode("ascii")
    if url_safe:
        out = out.rstrip("=")
    return out


@converter(
    method="GET",
    category="encoding",
    example_input={"text": "aGVsbG8=", "url_safe": False},
    example_output="hello",
)
def base64_decode(text: str, url_safe: bool = False) -> str:
    """把 Base64 文本解码回原文。"""
    data = text.strip()
    if url_safe:
        data = data.replace("-", "+").replace("_", "/")
        data += "=" * (-len(data) % 4)
    try:
        raw = base64.b64decode(data, validate=True)
    except Exception as exc:
        raise ValueError(f"invalid base64 input: {exc}") from exc
    return raw.decode("utf-8")
