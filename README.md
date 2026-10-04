# ConvertorAPI

**Many convertors, one API.**

ConvertorAPI 是一个用 Python 写的自托管 convertor 服务：把散落在各处的格式转换集中到一个 HTTP API 里。
现在内置 Base64、JSON / YAML、sing-box 订阅迁移，以及 WireGuard → sing-box，以后会越来越多 —— 每加一个 convertor，只要写一个普通函数。

[![Python](https://img.shields.io/badge/python-3.11%2B-blue.svg)](https://www.python.org/)
[![FastAPI](https://img.shields.io/badge/FastAPI-0.115%2B-009688.svg)](https://fastapi.tiangolo.com/)
[![Pydantic](https://img.shields.io/badge/Pydantic-v2-e92063.svg)](https://docs.pydantic.dev/)
[![License](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)

## 已有的 convertor

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/base64_encode` | 文本 → Base64；`url_safe=true` 使用 URL 安全字符集 |
| GET | `/base64_decode` | Base64 → 文本 |
| POST | `/json_to_yaml` | JSON 文本 → YAML 文本 |
| POST | `/yaml_to_json` | YAML 文本 → JSON 对象（顶层须为 mapping） |
| POST | `/singbox_migrate` | 迁移一份 sing-box 配置：legacy DNS server、rule_set 下载字段、入站旧字段 → 1.14+ 新格式 |
| GET | `/singbox_fix` | 拉取 sing-box 订阅、补齐面板漏发的节点、迁移后返回，可直接当订阅地址用 |
| POST | `/wireguard2singbox` | WireGuard 客户端 `.conf` → sing-box 配置（1.14+ 的 wireguard endpoint）；`full=false` 只给 endpoint |

`GET /convertors` 返回机器可读的完整清单；`GET /convertors/{name}` 返回单个 convertor 的
参数、Content-Type、JSON Schema 和示例。

## 快速开始

```bash
git clone https://github.com/fb0sh/ConvertorAPI.git
cd ConvertorAPI
uv sync
uv run uvicorn api.main:app --reload
```

打开 <http://127.0.0.1:8000/docs> 是 Swagger UI，<http://127.0.0.1:8000/> 是 Markdown 文档。

> 需要 [uv](https://docs.astral.sh/uv/)；没有的话：`curl -LsSf https://astral.sh/uv/install.sh | sh`

## 用起来是什么样

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

## 修 sing-box 订阅（1.14 迁移）

sing-box 1.12 移除 GeoIP/Geosite 条件、1.14 移除 legacy DNS server 旧写法、1.16 计划移除
`download_detour`。服务商模板更新不及时，订阅就会「能导入、启动不了」。这两个 convertor 负责修好它：

| 旧写法（服务商下发） | 新写法（1.14+） |
| --- | --- |
| `dns.servers[].address` 字符串 | `{type: udp/tcp/tls/https/quic/http3/local, server, server_port, path}` |
| `rcode://refused` 型 DNS server | 删掉该 server，DNS 规则改 `action: "predefined"` + `rcode` |
| DNS 规则省略 `action` | 补 `action: "route"` |
| DNS 规则里的 `outbound` 条件 | 移除，改用 `route.default_domain_resolver` |
| 规则里的 `geoip` / `geosite` 条件 | 自动生成 `route.rule_set` 定义并改成 `rule_set` 引用 |
| `rule_set[].download_detour` | `http_client: {detour: ...}` |
| 入站 `sniff` / `sniff_timeout` | route 规则 `action: "sniff"` |
| 入站 `domain_strategy` | route 规则 `action: "resolve"` + `strategy` |
| 入站 `sniff_override_destination` | 新版无对应字段，删除 |
| DNS server 的 `detour` 指向「空 direct 出站」 | 给该 direct 出站补 `domain_resolver`（指向一个用 IP 直连的 DNS server） |

> `sing-box check` 只校验 schema。最后一条属于**运行时**检查，`check` 会通过、启动时才报
> `detour to an empty direct outbound makes no sense`，所以别只信 `check`，起一次才算数。

迁移是**幂等**的；节点出站（trojan 等）不做任何改动，只会在必要时给 `direct` 出站补一个字段。

手上已经有一份配置时，用 `POST /singbox_migrate`：

```bash
curl -X POST http://127.0.0.1:8000/singbox_migrate \
  -H "Content-Type: application/json" \
  -d "{\"config\": $(cat old-config.json)}" > fixed-config.json
```

想一劳永逸，就把订阅地址换成 fixer（`GET /singbox_fix`），客户端拿到的一直是迁移好的配置：

```bash
curl -G http://127.0.0.1:8000/singbox_fix \
  --data-urlencode "url=https://你的机场/api/v1/client/subscribe?token=XXX" > fixed.json
```

GUI 里订阅地址直接填（URL 需要转义）：

```
http://127.0.0.1:8000/singbox_fix?url=https%3A%2F%2F你的机场%2Fapi%2Fv1%2Fclient%2Fsubscribe%3Ftoken%3DXXX
```

几个实测出来的注意点：

- **同一个订阅会按 User-Agent 下发不同格式**：sing-box 客户端拿到 JSON 配置，Clash 拿到 YAML，v2rayN 拿到 base64 节点列表。`singbox_fix` 默认带 `ua=sing-box/1.14.0`，源站不认这个 UA 时可以换 `ua` 参数。
- **源站普遍有 UA 白名单和频率限制**，所以 fixer 会缓存回源结果（默认跟随 `profile-update-interval`，限制在 1 分钟到 6 小时之间），不会每次刷新都打源站。
- 订阅 token 会出现在 fixer 的 URL 和访问日志里，建议只在本机或内网使用。

### 面板的 sing-box 模板会少发节点

实测同一个订阅：sing-box 版配置里只有 33 个 trojan 节点，而传统客户端格式（base64 节点列表）有 **65 个** —— 33 trojan + 32 个 `anytls`，面板的 sing-box 模板根本不下发 anytls（Clash/Mihomo 模板同样只有 33 个）。

所以 `singbox_fix` 默认会**再拉一份节点列表，把缺的节点补进配置**：按 `type + server + port` 去重（trojan 不会重复，只补 anytls），并把这些节点加进已有的 `selector` / `urltest` 组。

| 参数 | 默认值 | 说明 |
| --- | --- | --- |
| `url` | 必填 | 订阅地址 |
| `ua` | `sing-box/1.14.0` | 拉 sing-box 版配置用的 UA |
| `merge_nodes` | `true` | 是否合并节点列表里多出来的节点 |
| `nodes_ua` | `v2rayN/6.0` | 拉节点列表用的 UA |

代价是每个缓存周期向源站发 2 次请求（配置 + 节点列表），同样由缓存兜住；节点列表拉不到时会退回 sing-box 版，订阅本身仍然可用。不想合并就加 `&merge_nodes=false`。

## WireGuard → sing-box

手上有一份 WireGuard 客户端 `.conf`（wg-quick 或手机 App 导出的那种），可以直接转成 sing-box 配置：

```bash
python3 -c 'import json;print(json.dumps({"conf":open("phone.conf").read()}))' > req.json

curl -X POST http://127.0.0.1:8000/wireguard2singbox \
  -H "Content-Type: application/json" -d @req.json > wg.json

sing-box check -c wg.json
```

转出来的是一份完整配置：`mixed` 入站（默认 `127.0.0.1:2080`）+ `endpoints[]` 里的 wireguard
endpoint + `direct` 出站。**sing-box 1.13 起旧的 `type: "wireguard"` outbound 已被删除**
（[deprecated](https://sing-box.sagernet.org/deprecated/)），所以这里只生成 `endpoints[]` 的写法；
`system` 默认 `false`，走用户态实现，不需要 root 或额外接口。

`.conf` 里的东西去了哪：

| WireGuard | sing-box |
| --- | --- |
| `[Interface] Address` | `endpoints[].address`（不带掩码自动补 `/32` / `/128`） |
| `[Interface] PrivateKey` | `endpoints[].private_key` |
| `[Interface] MTU` / `ListenPort` | `endpoints[].mtu`（缺省 1408）/ `endpoints[].listen_port` |
| `[Interface] DNS` | `dns.servers[]`（落在隧道网段内才加 `detour` 走隧道） |
| `[Peer] PublicKey` / `PresharedKey` | `peers[].public_key` / `peers[].pre_shared_key` |
| `[Peer] Endpoint` | `peers[].address` + `peers[].port`（不写端口按 51820） |
| `[Peer] AllowedIPs` | `peers[].allowed_ips`，同时翻译成路由 |
| `[Peer] PersistentKeepalive` | `peers[].persistent_keepalive_interval` |

**AllowedIPs 会当成路由用**，这点和 wg-quick 一致：只有 AllowedIPs 里的网段走隧道，其余走 `direct`。
所以 `AllowedIPs = 172.16.0.0/24`（只代理内网那种）转出来只有一条 `ip_cidr` 规则 +
`route.final: "direct"`；只有 `0.0.0.0/0` + `::/0` 全量接管时才整体默认走隧道。域名对端（如 WARP 的
`engage.cloudflareclient.com`）也认，会自己带上解析用的 `domain_resolver`。

| 参数 | 默认值 | 说明 |
| --- | --- | --- |
| `conf` | 必填 | WireGuard `.conf` 全文 |
| `tag` | `wireguard-out` | endpoint 的 tag，也是路由规则指向的出站 |
| `full` | `true` | `false` 时只返回那个 endpoint 对象，方便塞进已有配置 |
| `system` | `false` | 用系统 WireGuard 接口（需要 root），默认走用户态实现 |
| `mtu` | 空 | 覆盖 `[Interface] MTU` |
| `mixed_port` | `2080` | 完整配置里 `mixed` 入站的端口 |
| `resolver` | `dns-local` | 完整配置里本机 DNS server 的 tag，也用来解析对端域名 |

`full=false` 时只给 endpoint，不写 `domain_resolver` 和 `route`（你的配置里未必有 `dns-local`
这个 tag）；对端是域名的话自己补一个 `domain_resolver`。`Reserved`（Cloudflare WARP 需要的那 3 个
字节）也支持；未知选项会被忽略（例如 AmneziaWG 的 `Jc` / `S1` 等混淆参数）。段名 / 键名大小写不敏感，
`#` 之后是注释，和 wg-quick 一样。

> 生成的配置拿 sing-box 1.14.2 实测过 `check` 和 `run`。两个容易踩的坑顺手记一下：对端是域名时
> `domain_resolver` 必须写在 **endpoint** 上（写进 `peers[]` 会 `unknown field "domain_resolver"`
> 直接起不来）；DNS server 有 2 个以上时 1.14 强制要求 `route.default_domain_resolver`，否则报
> `missing route.default_domain_resolver ... is deprecated` 并拒绝启动 —— 生成时会补上。

## 添加一个 convertor

ConvertorAPI 的能力由一个一个 convertor 组成。每个 convertor 就是一个普通 Python 函数：
**函数名 = 路由名**，**参数名 = 请求字段名**，**返回值 = 响应本体**。

在 `convertors/` 下新建一个 `.py` 文件，写好函数加装饰器就完事 —— 不需要注册，不需要改其他任何文件：

```python
from core.decorator import convertor


@convertor(
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
| `category` | `str` | `"text"` | 分组标签，用于 `/convertors?category=` 过滤和 Swagger 标签 |
| `produces` | `str \| None` | `None` | 显式指定响应 Content-Type，默认由返回类型推断 |
| `example_input` | `Any` | `None` | 示例入参，展示在 `/convertors/{name}` |
| `example_output` | `Any` | `None` | 示例出参，展示在 `/convertors/{name}` |

### 输入规则

- `GET` convertor：参数从 query string 读取
- `POST` convertor：参数从 JSON body 读取
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
@convertor(method="POST", category="text")
def parse_int(text: str) -> int:
    return int(text)   # text="abc" → 400
```

### 保留字

以下名字不能作为路由首段（也不能作为函数名）：`convertors`、`docs`、`redoc`、`openapi.json`、`favicon.ico`。
同名函数重复注册、或两条路由的 `method + path` 完全相同，启动时直接报错，服务不会带着坏路由起来。

## 元数据路由

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/` | Markdown 文档：元数据路由 + 当前 convertor + 开发教程 |
| GET | `/convertors` | convertor 列表，支持 `?category=` 过滤 |
| GET | `/convertors/{name}` | 单个 convertor 的完整用法（参数、Content-Type、JSON Schema、示例） |
| GET | `/docs` | Swagger UI |

## 项目结构

```
.
├── pyproject.toml
├── uv.lock
├── core/
│   ├── registry.py     # ConvertorEntry + Registry：convertor 注册表
│   ├── decorator.py    # @convertor：把函数登记进注册表
│   └── docs.py         # TUTORIAL、返回类型推断、GET / 的 Markdown 渲染
├── api/
│   ├── main.py         # FastAPI 应用、元数据路由、启动时扫描 convertors
│   └── mount.py        # 按函数签名生成请求模型并把 convertor 挂成路由
└── convertors/
    ├── base64_conv.py  # base64_encode / base64_decode
    ├── json_yaml.py    # json_to_yaml / yaml_to_json
    ├── singbox_fix.py  # singbox_migrate / singbox_fix（含迁移引擎）
    └── wireguard2singbox.py  # wireguard2singbox（WireGuard .conf → sing-box 1.14+ endpoint）
```

## 技术栈

Python 3.11+ · FastAPI · Pydantic v2 · PyYAML · uvicorn · uv

## 许可证

[MIT](LICENSE)
