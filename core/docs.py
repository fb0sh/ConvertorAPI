"""文档渲染：开发教程、返回类型推断、以及 GET / 的 Markdown 文档。"""

import inspect
from typing import Any, Callable, get_type_hints

from pydantic import BaseModel

from core.registry import registry

TUTORIAL = """## 如何开发一个 Convertor

一个转换器就是一个普通 Python 函数：**函数名 = 路由名**，**参数名 = 请求字段名**，
**返回值 = 响应本体**。

### 最小示例

```python
from core.decorator import converter


@converter(method="GET", category="text")
def shout(text: str) -> str:
    \"\"\"把文本转成大写。\"\"\"
    return text.upper()
```

把文件放进 `converters/` 目录即可，不需要改任何其他文件：服务启动时会自动扫描
`converters` 包、导入每个模块（触发装饰器），然后把函数挂载成路由。
上面的例子可以通过 `GET /shout?text=hi` 访问。

### 装饰器参数

| 参数 | 类型 | 默认值 | 说明 |
| --- | --- | --- | --- |
| `method` | `str` | `"POST"` | HTTP 方法，大小写不敏感 |
| `path` | `str \\| None` | `None` | 路由路径，默认 `/<函数名>` |
| `category` | `str` | `"text"` | 分组标签，用于 `/converters?category=` 过滤和 Swagger 标签 |
| `produces` | `str \\| None` | `None` | 显式指定响应 Content-Type，默认由返回类型推断 |
| `example_input` | `Any` | `None` | 示例入参，展示在 `/converters/{name}` |
| `example_output` | `Any` | `None` | 示例出参，展示在 `/converters/{name}` |

保留字（不能作为路由首段，也不能作为函数名）：`converters`、`docs`、`redoc`、
`openapi.json`、`favicon.ico`。

同一个函数名注册两次，或者两条路由的 method + path 完全相同，都会在进程启动时
直接抛出 `ValueError`，服务不会带着坏路由起来。

### 输入规则

- 参数名就是请求字段名。
- `GET` 转换器：参数从 query string 读取。
- `POST` 转换器：参数从 JSON body 读取。
- 带类型注解的参数是必填的；带默认值的参数是选填的。
- 支持 `str`、`int`、`float`、`bool`、`list`、`dict` 等常见类型。

### 输出规则

| 返回类型 | Content-Type |
| --- | --- |
| `str` | `text/plain; charset=utf-8` |
| `dict` / `list` | `application/json` |
| Pydantic 模型 | `application/json`（附 JSON Schema） |
| `bytes` | `application/octet-stream` |

返回值就是响应本体，不会再包一层 `{"result": ...}`。想改 Content-Type，用装饰器的
`produces` 参数显式覆盖。

### 错误处理

函数抛出的任何异常都会被自动转成 `400 Bad Request`，异常信息放在响应的 `detail`
字段里，不需要自己写 try/except：

```python
@converter(method="POST", category="text")
def parse_int(text: str) -> int:
    return int(text)  # text="abc" 时返回 400
```

### 完整例子

```python
import base64

from core.decorator import converter


@converter(
    method="GET",
    category="encoding",
    example_input={"text": "hello", "url_safe": False},
    example_output="aGVsbG8=",
)
def base64_encode(text: str, url_safe: bool = False) -> str:
    \"\"\"把文本进行 Base64 编码。\"\"\"
    raw = text.encode("utf-8")
    encoded = base64.urlsafe_b64encode(raw) if url_safe else base64.b64encode(raw)
    return encoded.decode("ascii")
```
"""


def infer_output(fn: Callable[..., Any]) -> tuple[str, dict[str, Any] | None]:
    """按函数的返回类型注解推断 (content_type, json_schema)。"""
    try:
        hints = get_type_hints(fn)
    except Exception:  # 注解无法解析时退回默认值
        hints = {}
    ret = hints.get("return")

    if ret is bytes:
        return "application/octet-stream", None
    if isinstance(ret, type) and issubclass(ret, BaseModel):
        return "application/json", ret.model_json_schema()
    if ret is dict or (isinstance(ret, type) and issubclass(ret, dict)):
        return "application/json", {"type": "object"}
    if ret is list or (isinstance(ret, type) and issubclass(ret, list)):
        return "application/json", {"type": "array"}
    if ret is str:
        return "text/plain; charset=utf-8", None
    return "text/plain; charset=utf-8", None


def render_api_doc() -> str:
    """渲染 GET / 返回的 Markdown 文档。"""
    lines = [
        "# ConvertorAPI",
        "",
        "A function is a converter.",
        "",
        "## 元数据路由",
        "",
        "| 方法 | 路径 | 说明 |",
        "| --- | --- | --- |",
        "| GET | `/` | 本页 Markdown 文档 |",
        "| GET | `/converters` | 转换器列表，支持 `?category=` 过滤 |",
        "| GET | `/converters/{name}` | 单个转换器的完整用法 |",
        "| GET | `/docs` | Swagger UI（FastAPI 内置） |",
        "",
        "## 当前转换器",
        "",
        "| 方法 | 路径 | 类别 | 输出 | 说明 |",
        "| --- | --- | --- | --- | --- |",
    ]

    for entry in registry.all():
        produces = entry.produces or infer_output(entry.fn)[0]
        doc_lines = (inspect.getdoc(entry.fn) or "").strip().splitlines()
        summary = doc_lines[0] if doc_lines else entry.name
        lines.append(
            f"| {entry.method} | `{entry.path}` | {entry.category} | "
            f"`{produces}` | {summary} |"
        )

    lines += ["", TUTORIAL]
    return "\n".join(lines) + "\n"
