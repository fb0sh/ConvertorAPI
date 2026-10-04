"""WireGuard → sing-box convertor：把 wg-quick 风格的 `.conf` 转成 sing-box 1.14+ 配置。

sing-box 1.13 起旧的 `type: "wireguard"` outbound 已被移除，WireGuard 只能写成
`endpoints[]`，所以这里生成的永远是 endpoint（`system` 默认 false，走 sing-box 自带的
用户态实现，不需要 root 或额外接口）。

WireGuard 的 AllowedIPs 是「哪些目标走隧道」的路由语义，所以它不会只写进 peer，
还会翻译成 route 规则：只有 AllowedIPs 里的网段走隧道，其余按 `route.final` 走 direct
（和 wg-quick 一致）；只有当 AllowedIPs 覆盖 `0.0.0.0/0` 和 `::/0` 时才整体默认走隧道。
"""

import ipaddress
import re
from dataclasses import dataclass, field
from typing import Any

from core.decorator import convertor

DEFAULT_TAG = "wireguard-out"
DEFAULT_MIXED_PORT = 2080
DEFAULT_WG_PORT = 51820
DEFAULT_MTU = 1408
# 解析对端域名 / 兜底 DNS 用的本地解析器，不走隧道，避免「解析隧道地址本身要先进隧道」
LOCAL_RESOLVER_TAG = "dns-local"
FULL_TUNNEL = {"0.0.0.0/0", "::/0"}


def _strip_comment(line: str) -> str:
    """去掉行内/整行注释：`#` 之后的内容（wg-quick 也是这么切的）。"""
    return line.split("#", 1)[0].strip()


def _split_values(values: list[str] | None) -> list[str]:
    """把 `Address` / `AllowedIPs` / `DNS` 这类可重复、可逗号分隔的字段摊平。"""
    out: list[str] = []
    for value in values or []:
        for part in re.split(r"[,\s]+", value):
            if part:
                out.append(part)
    return out


def _last(options: dict[str, list[str]], key: str) -> str | None:
    """取某个选项最后出现的值（同名选项后者覆盖前者）。"""
    values = options.get(key)
    return values[-1] if values else None


def _int_option(options: dict[str, list[str]], key: str, label: str) -> int | None:
    raw = _last(options, key)
    if raw is None or not raw.strip():
        return None
    try:
        return int(raw)
    except ValueError as exc:
        raise ValueError(f"{label} 不是整数：{raw!r}") from exc


def _prefix(value: str, *, interface: bool) -> str:
    """规范化 CIDR：interface 地址保留主机位，其它按网络地址归整。"""
    try:
        if interface:
            return str(ipaddress.ip_interface(value))
        return str(ipaddress.ip_network(value, strict=False))
    except ValueError as exc:
        raise ValueError(f"不是合法的 IP / CIDR：{value!r}（{exc}）") from exc


def _is_ip(host: str) -> bool:
    try:
        ipaddress.ip_address(host)
    except ValueError:
        return False
    return True


def _split_host_port(value: str) -> tuple[str, int | None]:
    """解析 `主机:端口` / `[v6]:端口` / 裸主机（裸 IPv6 视为不带端口）。"""
    text = value.strip()
    if not text:
        raise ValueError("地址是空的")
    host, port_text = text, ""
    if text.startswith("["):
        end = text.find("]")
        if end == -1:
            raise ValueError(f"IPv6 地址缺少右括号：{text!r}")
        host = text[1:end]
        rest = text[end + 1:].strip()
        if rest.startswith(":"):
            port_text = rest[1:]
        elif rest:
            raise ValueError(f"无法解析的地址：{text!r}")
    elif text.count(":") == 1:
        host, _, port_text = text.partition(":")
    # 多个冒号 = 裸 IPv6、零个冒号 = 裸主机名，都没有端口

    host = host.strip()
    if not host:
        raise ValueError(f"地址缺少主机部分：{text!r}")

    if not port_text.strip():
        return host, None
    try:
        port = int(port_text.strip())
    except ValueError as exc:
        raise ValueError(f"端口不是整数：{text!r}") from exc
    if not 1 <= port <= 65535:
        raise ValueError(f"端口超出范围：{port}")
    return host, port


def _in_prefixes(host: str, prefixes: list[str]) -> bool:
    """host 是字面 IP 且在给定网段里时才为真（域名交给 DNS 判断）。"""
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return False
    for prefix in prefixes:
        try:
            if ip in ipaddress.ip_network(prefix):
                return True
        except ValueError:
            continue
    return False


def _routes_everything(prefixes: list[str]) -> bool:
    return FULL_TUNNEL.issubset(set(prefixes))


@dataclass
class WireGuardConf:
    """解析后的 WireGuard 客户端配置。"""

    addresses: list[str]
    private_key: str
    peers: list[dict[str, Any]]
    allowed_ips: list[str]
    dns: list[str] = field(default_factory=list)
    mtu: int = DEFAULT_MTU
    listen_port: int | None = None


def parse_wireguard_config(conf: str) -> WireGuardConf:
    """解析 .conf 文本：只认 [Interface] / [Peer] 两段，未知选项忽略。"""
    interface: dict[str, list[str]] | None = None
    peer_options: list[dict[str, list[str]]] = []
    current: dict[str, list[str]] | None = None

    for raw in conf.splitlines():
        line = _strip_comment(raw)
        if not line:
            continue
        if line.startswith("[") and line.endswith("]"):
            section = line[1:-1].strip().lower()
            if section == "interface":
                if interface is not None:
                    raise ValueError("只支持一个 [Interface] 段")
                interface = {}
                current = interface
            elif section == "peer":
                peer_options.append({})
                current = peer_options[-1]
            else:
                raise ValueError(f"无法识别的段：{line!r}（只支持 [Interface] 和 [Peer]）")
            continue

        if current is None:
            raise ValueError(f"配置项出现在任何段之前：{line!r}")
        key, sep, value = line.partition("=")
        key = key.strip().lower()
        if not sep or not key:
            raise ValueError(f"无法解析的行：{line!r}")
        current.setdefault(key, []).append(value.strip())

    if interface is None:
        raise ValueError("配置里没有 [Interface] 段")
    if not peer_options:
        raise ValueError("配置里没有 [Peer] 段")

    private_key = _last(interface, "privatekey")
    if not private_key:
        raise ValueError("[Interface] 缺少 PrivateKey")

    addresses = [
        _prefix(value, interface=True)
        for value in _split_values(interface.get("address"))
    ]
    if not addresses:
        raise ValueError("[Interface] 缺少 Address")

    parsed = WireGuardConf(
        addresses=addresses,
        private_key=private_key,
        peers=[],
        allowed_ips=[],
        dns=_split_values(interface.get("dns")),
        mtu=_int_option(interface, "mtu", "MTU") or DEFAULT_MTU,
        listen_port=_int_option(interface, "listenport", "ListenPort"),
    )
    for index, options in enumerate(peer_options, start=1):
        parsed.peers.append(_build_peer(options, index))
        for prefix in parsed.peers[-1]["allowed_ips"]:
            if prefix not in parsed.allowed_ips:
                parsed.allowed_ips.append(prefix)
    return parsed


def _build_peer(options: dict[str, list[str]], index: int) -> dict[str, Any]:
    """一个 [Peer] → sing-box endpoint 的 peers[] 条目。"""
    public_key = _last(options, "publickey")
    if not public_key:
        raise ValueError(f"第 {index} 个 [Peer] 缺少 PublicKey")

    endpoint = _last(options, "endpoint")
    if not endpoint:
        raise ValueError(f"第 {index} 个 [Peer] 缺少 Endpoint")
    address, port = _split_host_port(endpoint)

    allowed_ips = [
        _prefix(value, interface=False)
        for value in _split_values(options.get("allowedips"))
    ]
    if not allowed_ips:
        raise ValueError(f"第 {index} 个 [Peer] 缺少 AllowedIPs")

    peer: dict[str, Any] = {
        "address": address,
        "port": port or DEFAULT_WG_PORT,
        "public_key": public_key,
        "allowed_ips": allowed_ips,
    }

    pre_shared_key = _last(options, "presharedkey")
    if pre_shared_key:
        peer["pre_shared_key"] = pre_shared_key

    keepalive = _last(options, "persistentkeepalive")
    if keepalive:
        try:
            seconds = int(keepalive)
        except ValueError as exc:
            raise ValueError(
                f"第 {index} 个 [Peer] 的 PersistentKeepalive 不是整数：{keepalive!r}"
            ) from exc
        if seconds > 0:
            peer["persistent_keepalive_interval"] = seconds

    reserved = _split_values(options.get("reserved"))  # Cloudflare WARP
    if reserved:
        try:
            peer["reserved"] = [int(part) for part in reserved]
        except ValueError as exc:
            raise ValueError(
                f"第 {index} 个 [Peer] 的 Reserved 必须是整数：{reserved!r}"
            ) from exc

    return peer


def build_endpoint(
    conf: WireGuardConf,
    *,
    tag: str = DEFAULT_TAG,
    system: bool = False,
    mtu: int | None = None,
    resolver: str | None = None,
) -> dict[str, Any]:
    """把解析结果写成 `endpoints[]` 里的一个 wireguard endpoint。"""
    endpoint: dict[str, Any] = {
        "type": "wireguard",
        "tag": tag,
        "system": system,
        "mtu": mtu or conf.mtu,
        "address": list(conf.addresses),
        "private_key": conf.private_key,
    }
    if conf.listen_port:
        endpoint["listen_port"] = conf.listen_port

    endpoint["peers"] = [dict(peer) for peer in conf.peers]
    # 对端写域名时，解析它得走隧道外，否则要先进隧道再解析 → 死循环。
    # domain_resolver 是 endpoint（dial fields）上的字段，不能写在 peer 上。
    if resolver and any(not _is_ip(peer["address"]) for peer in conf.peers):
        endpoint["domain_resolver"] = resolver
    return endpoint


def _dns_server(value: str, tag: str, resolver: str | None, detour: str | None) -> dict[str, Any]:
    host, port = _split_host_port(value)
    server: dict[str, Any] = {"type": "udp", "tag": tag, "server": host}
    if port:
        server["server_port"] = port
    if not _is_ip(host) and resolver:
        server["domain_resolver"] = resolver
    if detour:
        server["detour"] = detour
    return server


def build_config(
    conf: str,
    *,
    tag: str = DEFAULT_TAG,
    system: bool = False,
    mtu: int | None = None,
    mixed_port: int = DEFAULT_MIXED_PORT,
    resolver: str = LOCAL_RESOLVER_TAG,
) -> dict[str, Any]:
    """生成一份可以直接导入 sing-box 的完整配置。"""
    parsed = parse_wireguard_config(conf)
    endpoint = build_endpoint(
        parsed, tag=tag, system=system, mtu=mtu, resolver=resolver
    )

    # conf 里的 DNS：只有落在隧道网段内才用隧道去问，否则直连（否则会打进一个到不了的地址）
    servers: list[dict[str, Any]] = []
    for index, value in enumerate(parsed.dns, start=1):
        host, _ = _split_host_port(value)
        in_tunnel = _in_prefixes(host, parsed.allowed_ips)
        name = f"dns-{index}" if len(parsed.dns) > 1 else "dns"
        servers.append(_dns_server(value, name, resolver, tag if in_tunnel else None))
    servers.append({"type": "local", "tag": resolver})
    if not parsed.dns:
        dns_final = resolver
    elif len(parsed.dns) == 1:
        dns_final = "dns"
    else:
        dns_final = "dns-1"

    rules: list[dict[str, Any]] = [
        {"action": "sniff"},
        {"protocol": "dns", "action": "hijack-dns"},
    ]
    if _routes_everything(parsed.allowed_ips):
        final = tag
    else:
        rules.append({"ip_cidr": list(parsed.allowed_ips), "action": "route", "outbound": tag})
        final = "direct"

    return {
        "log": {"level": "info", "timestamp": True},
        "dns": {"servers": servers, "final": dns_final},
        "inbounds": [
            {
                "type": "mixed",
                "tag": "mixed-in",
                "listen": "127.0.0.1",
                "listen_port": mixed_port,
            }
        ],
        "endpoints": [endpoint],
        "outbounds": [{"type": "direct", "tag": "direct"}],
        "route": {
            "rules": rules,
            # 有几个 DNS server 时，dial 字段必须能指名一个解析器，否则 1.14 直接启动失败
            "default_domain_resolver": resolver,
            "final": final,
        },
    }


EXAMPLE_INPUT = {
    # 示例里的密钥是一次性生成的假密钥，IP 用的是 RFC 5737 文档专用段
    "conf": (
        "[Interface]\n"
        "Address = 172.16.0.2/32\n"
        "PrivateKey = aAkReOIpfRX/BJm06x/9RBfc/WeK1cOa+2p7hT3ry1Q=\n"
        "\n"
        "[Peer]\n"
        "AllowedIPs = 172.16.0.0/24\n"
        "Endpoint = 198.51.100.10:51820\n"
        "PersistentKeepalive = 25\n"
        "PublicKey = c3nIxGx4U/xVcoDeyJzC2UN3MXRePXKEV6UWcdQ1DhE=\n"
    ),
    "tag": "wireguard-out",
    "full": True,
    "system": False,
    "mtu": None,
    "mixed_port": 2080,
    "resolver": "dns-local",
}

EXAMPLE_OUTPUT = {
    "log": {"level": "info", "timestamp": True},
    "dns": {
        "servers": [{"type": "local", "tag": "dns-local"}],
        "final": "dns-local",
    },
    "inbounds": [
        {"type": "mixed", "tag": "mixed-in", "listen": "127.0.0.1", "listen_port": 2080}
    ],
    "endpoints": [
        {
            "type": "wireguard",
            "tag": "wireguard-out",
            "system": False,
            "mtu": 1408,
            "address": ["172.16.0.2/32"],
            "private_key": "<PrivateKey>",
            "peers": [
                {
                    "address": "198.51.100.10",
                    "port": 51820,
                    "public_key": "<PublicKey>",
                    "allowed_ips": ["172.16.0.0/24"],
                    "persistent_keepalive_interval": 25,
                }
            ],
        }
    ],
    "outbounds": [{"type": "direct", "tag": "direct"}],
    "route": {
        "rules": [
            {"action": "sniff"},
            {"protocol": "dns", "action": "hijack-dns"},
            {
                "ip_cidr": ["172.16.0.0/24"],
                "action": "route",
                "outbound": "wireguard-out",
            },
        ],
        "default_domain_resolver": "dns-local",
        "final": "direct",
    },
}


@convertor(
    method="POST",
    category="singbox",
    produces="application/json",
    example_input=EXAMPLE_INPUT,
    example_output=EXAMPLE_OUTPUT,
)
def wireguard2singbox(
    conf: str,
    tag: str = DEFAULT_TAG,
    full: bool = True,
    system: bool = False,
    mtu: int | None = None,
    mixed_port: int = DEFAULT_MIXED_PORT,
    resolver: str = LOCAL_RESOLVER_TAG,
) -> dict:
    """把 WireGuard 客户端 .conf 转成 sing-box 配置（1.14+ 的 wireguard endpoint）；full=false 时只给 endpoint。"""
    if full:
        return build_config(
            conf,
            tag=tag,
            system=system,
            mtu=mtu,
            mixed_port=mixed_port,
            resolver=resolver,
        )
    # 只要 endpoint 时不知道外面有哪些 DNS tag，不写 domain_resolver，避免指向不存在的 server
    return build_endpoint(
        parse_wireguard_config(conf), tag=tag, system=system, mtu=mtu
    )
