# ConvertorAPI

**A function is a converter.**

给一个普通 Python 函数加上 `@converter` 装饰器，它就变成一个 HTTP 接口 —— 不用写路由、不用写请求模型、不用写响应封装。

[![Python](https://img.shields.io/badge/python-3.11%2B-blue.svg)](https://www.python.org/)
[![FastAPI](https://img.shields.io/badge/FastAPI-0.115%2B-009688.svg)](https://fastapi.tiangolo.com/)
[![Pydantic](https://img.shields.io/badge/Pydantic-v2-e92063.svg)](https://docs.pydantic.dev/)
[![License](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)

## 它解决什么

想把一段转换逻辑暴露成 HTTP 接口，通常得写路由、参数校验、序列化、错误处理，一堆胶水代码。ConvertorAPI 把这些收进一个装饰器里：

- **函数名 = 路由名**，**参数名 = 请求字段名**，**返回值 = 响应本体**
- 参数类型用 Python 类型注解声明，自动变成请求校验
- 返回值类型决定 Content-Type，自动序列化
- 函数里抛的任何异常自动转成 `400`
- 加一个 `.py` 文件就多一个接口，不用到处注册

## 快速开始

```bash
git clone https://github.com/fb0sh/ConvertorAPI.git
cd ConvertorAPI
uv sync
uv run uvicorn api.main:app --reload
```

打开 <http://127.0.0.1:8000/docs> 是 Swagger UI，<http://127.0.0.1:8000/> 是 Markdown 文档。

> 需要 [uv](https://docs.astral.sh/uv/)；没有的话：`curl -LsSf https://astral.sh/uv/install.sh | sh`

## 试试看

```bash
# 文本 → Base64
curl "http://127.0.0.1:8000/base64_encode?text=hello"
# aGVsbG8=

# Base64 → 文本
curl "http://127.0.0.1:8000/base64_decode?text=aGVsbG8="
# hello

# JSON → YAML
curl -X POST http://127.0.0.1:8000/json_to_yaml \
  -H "Content-Type: application/json" \
  -d '{"json_text":"{\"name\":\"alice\",\"age\":30}"}'
# name: alice
# age: 30

# YAML → JSON
curl -X POST http://127.0.0.1:8000/yaml_to_json \
  -H "Content-Type: application/json" \
  -d '{"yaml_text":"name: alice\nage: 30\n"}'
# {"name": "alice", "age": 30}
```

## 内置转换器

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/base64_encode` | 文本 → Base64；`url_safe=true` 使用 URL 安全字符集 |
| GET | `/base64_decode` | Base64 → 文本 |
| POST | `/json_to_yaml` | JSON 文本 → YAML 文本 |
| POST | `/yaml_to_json` | YAML 文本 → JSON 对象（顶层须为 mapping） |

## 写一个 Convertor

在 `converters/` 下新建一个 `.py` 文件，写好函数加装饰器就完事，不需要改其他任何文件：

```python
from core.decorator import converter


@converter(
    method="GET",
    category="text",
    example_input={"text": "hello"},
    example_output="HELLO",
)
def shout(text: str) -> str:
    """把文本转成大写。"""
    return text.upper()
```

`GET /shout?text=hello` → `HELLO`。

### 装饰器参数

| 参数 | 类型 | 默认值 | 说明 |
| --- | --- | --- | --- |
| `method` | `str` | `"POST"` | HTTP 方法，大小写不敏感 |
| `path` | `str \| None` | `None` | 路由路径，默认 `/<函数名>` |
| `category` | `str` | `"text"` | 分组标签，用于 `/converters?category=` 过滤和 Swagger 标签 |
| `produces` | `str \| None` | `None` | 显式指定响应 Content-Type，默认由返回类型推断 |
| `example_input` | `Any` | `None` | 示例入参，展示在 `/converters/{name}` |
| `example_output` | `Any` | `None` | 示例出参，展示在 `/converters/{name}` |

### 输入规则

- `GET` 转换器：参数从 query string 读取
- `POST` 转换器：参数从 JSON body 读取
- 带默认值的参数是选填的，其余必填
- 支持 `str`、`int`、`float`、`bool`、`list`、`dict` 等常见类型

### 输出规则

| 返回类型 | Content-Type |
| --- | --- |
| `str` | `text/plain; charset=utf-8` |
| `dict` / `list` | `application/json` |
| Pydantic 模型 | `application/json`（附 JSON Schema） |
| `bytes` | `application/octet-stream` |

返回值就是响应本体，不会再包一层 `{"result": ...}`。想换 Content-Type，用装饰器的 `produces` 覆盖。

### 错误处理

函数抛出的异常会自动变成 `400 Bad Request`，异常信息放在 `detail` 里：

```python
@converter(method="POST", category="text")
def parse_int(text: str) -> int:
    return int(text)   # text="abc" → 400
```

### 保留字

以下名字不能作为路由首段（也不能作为函数名）：`converters`、`docs`、`redoc`、`openapi.json`、`favicon.ico`。
同名函数重复注册、或两条路由的 `method + path` 完全相同，启动时直接报错，服务不会带着坏路由起来。

## 元数据路由

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/` | Markdown 文档：元数据路由 + 当前转换器 + 开发教程 |
| GET | `/converters` | 转换器列表，支持 `?category=` 过滤 |
| GET | `/converters/{name}` | 单个转换器的完整用法（参数、Content-Type、JSON Schema、示例） |
| GET | `/docs` | Swagger UI |

## 项目结构

```
.
├── pyproject.toml
├── uv.lock
├── core/
│   ├── registry.py     # ConvertorEntry + Registry：转换器注册表
│   ├── decorator.py    # @converter：把函数登记进注册表
│   └── docs.py         # TUTORIAL、返回类型推断、GET / 的 Markdown 渲染
├── api/
│   ├── main.py         # FastAPI 应用、元数据路由、启动时扫描 converters
│   └── mount.py        # 按函数签名生成请求模型并把转换器挂成路由
└── converters/
    ├── base64_conv.py  # base64_encode / base64_decode
    └── json_yaml.py    # json_to_yaml / yaml_to_json
```

## 技术栈

Python 3.11+ · FastAPI · Pydantic v2 · PyYAML · uvicorn · uv

## 许可证

[MIT](LICENSE)
