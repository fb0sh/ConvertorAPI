"""sing-box 迁移 convertor：把订阅下发的旧格式配置修成 1.14+ 能启动的配置。

迁移规则来自官方 migration 文档（SagerNet/sing-box）：
  - 1.12：legacy DNS server 旧写法废弃；GeoIP/Geosite 条件移除，改用 rule_set
  - 1.14：legacy DNS server 旧格式移除；download_detour 废弃（计划 1.16 移除）
  - 1.13：入站（inbound）的 sniff / domain_strategy 等字段移除，改用 route 规则动作
这里一次性迁到最新形态，跑在 1.14/1.15 上都不会再报 legacy 错误。
"""

import copy
import ipaddress
import json
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from core.decorator import convertor

DEFAULT_UA = "sing-box/1.14.0"
FETCH_TIMEOUT_SECONDS = 20.0
CACHE_TTL_SECONDS = 300.0
CACHE_TTL_MIN = 60.0
CACHE_TTL_MAX = 6 * 3600.0

LEGACY_INBOUND_FIELDS = (
    "sniff",
    "sniff_timeout",
    "sniff_override_destination",
    "domain_strategy",
    "udp_disable_domain_unmapping",
)

GEOSITE_URL = "https://raw.githubusercontent.com/SagerNet/sing-geosite/rule-set/{tag}.srs"
GEOIP_URL = "https://raw.githubusercontent.com/SagerNet/sing-geoip/rule-set/{tag}.srs"

_SCHEME_TYPES = {"https": "https", "tls": "tls", "tcp": "tcp", "udp": "udp", "quic": "quic", "h3": "http3"}
_DEFAULT_PORTS = {"https": 443, "h3": 443, "tls": 853, "quic": 853}

# 源站普遍按 User-Agent 协商格式且有频率限制，命中过的订阅必须缓存
_cache: dict[str, tuple[float, dict[str, Any]]] = {}


# --------------------------------------------------------------------------- #
# 一、迁移引擎（纯函数，不碰网络，方便单测和离线使用）
# --------------------------------------------------------------------------- #

def _migrate_dns_server(server: Any, changes: list[str]) -> Any:
    """旧 DNS server（靠 address 字符串）→ 新格式（靠 type 字段）。"""
    if not isinstance(server, dict) or "address" not in server:
        return server

    address = str(server["address"])
    tag = server.get("tag")

    if address == "local":
        new: dict[str, Any] = {"type": "local"}
    else:
        url = urllib.parse.urlparse(address if "://" in address else f"udp://{address}")
        server_type = _SCHEME_TYPES.get(url.scheme, "udp")
        new = {"type": server_type, "server": url.hostname or address}
        port = url.port or _DEFAULT_PORTS.get(server_type)
        if port:
            new["server_port"] = port
        if server_type in ("https", "h3") and url.path and url.path != "/":
            new["path"] = url.path

    if tag:
        new["tag"] = tag
    for field in ("detour", "client_subnet", "strategy", "tls"):
        if field in server:
            new[field] = server[field]
    if "address_resolver" in server:  # 1.12 起改名
        new["domain_resolver"] = server["address_resolver"]

    changes.append(f"dns.servers[{tag}]: {address!r} → type={new['type']}")
    return new


def _rule_set_for(kind: str, code: str, rule_sets: list[Any], http_client: Any) -> str:
    """为旧 geosite/geoip 条件补一条 rule_set 定义，返回它的 tag。"""
    tag = f"{kind}-{code}"
    if not any(isinstance(rs, dict) and rs.get("tag") == tag for rs in rule_sets):
        entry: dict[str, Any] = {
            "type": "remote",
            "tag": tag,
            "format": "binary",
            "url": (GEOSITE_URL if kind == "geosite" else GEOIP_URL).format(tag=tag),
        }
        if http_client:
            entry["http_client"] = http_client
        rule_sets.append(entry)
    return tag


def _migrate_legacy_conditions(rule: dict[str, Any], kind: str, rule_sets: list[Any], http_client: Any, changes: list[str]) -> None:
    """把规则里的旧 geosite/geoip 条件换成 rule_set 引用。"""
    value = rule.pop(kind, None)
    if value is None:
        return
    codes = value if isinstance(value, list) else [value]
    tags = [_rule_set_for(kind, str(code), rule_sets, http_client) for code in codes]
    existing = rule.get("rule_set")
    rule["rule_set"] = (list(existing) if isinstance(existing, list) else [existing] if existing else []) + tags
    changes.append(f"规则 {kind}={value!r} → rule_set {tags}")


def _download_client(section: Any) -> dict[str, Any] | None:
    """旧 route.geosite / route.geoip 段里的 download_detour → http_client。"""
    if isinstance(section, dict) and section.get("download_detour"):
        return {"detour": section["download_detour"]}
    return None


def _is_ip_literal(value: str) -> bool:
    try:
        ipaddress.ip_address(value.strip("[]"))
        return True
    except ValueError:
        return False


def _bootstrap_resolver(servers: list[Any], direct_tags: set[str]) -> str | None:
    """挑一个「不需要域名解析就能连上」的 DNS server 当 bootstrap 解析器。

    type=local 或 server 是 IP 字面量的都算；优先挑那些自己就走这些 direct 出站的
    （不依赖代理，不会绕成解析环）。
    """
    candidates = [
        s for s in servers
        if isinstance(s, dict) and s.get("tag")
        and (s.get("type") == "local" or _is_ip_literal(str(s.get("server", ""))))
    ]
    if not candidates:
        return None
    preferred = [s for s in candidates if s.get("detour") in direct_tags]
    return str((preferred or candidates)[0]["tag"])


def _fix_empty_direct_detours(out: dict[str, Any], dns: Any, changes: list[str]) -> None:
    """DNS server 的 detour 指向「空 direct 出站」时修掉它 —— sing-box run 会在启动时拒绝。

    报错原文：start dns/https[local]: detour to an empty direct outbound makes no sense
    判定在 common/dialer/detour.go，配合 protocol/direct 的 IsEmpty()：
    direct 出站的 Dial Fields 为空即视为「空出站」，不能作为 detour 目标。
    注意 sing-box check 只验 schema，这条只有真正启动才会暴露。
    """
    outbounds = out.get("outbounds")
    if not isinstance(outbounds, list) or not isinstance(dns, dict):
        return

    by_tag = {o.get("tag"): o for o in outbounds if isinstance(o, dict) and o.get("tag")}
    empty_directs: list[dict[str, Any]] = []
    for server in dns.get("servers") or []:
        if not isinstance(server, dict):
            continue
        target = by_tag.get(server.get("detour"))
        # 只有 {type, tag} 才算空（有 override_address / domain_resolver 等 Dial Fields 就不空）
        if isinstance(target, dict) and target.get("type") == "direct" and set(target) <= {"type", "tag"}:
            if target not in empty_directs:
                empty_directs.append(target)

    if not empty_directs:
        return

    resolver = _bootstrap_resolver(dns.get("servers") or [], {t.get("tag") for t in empty_directs})
    for target in empty_directs:
        if resolver and resolver != target.get("tag"):
            target["domain_resolver"] = resolver
            changes.append(
                f"outbounds[{target.get('tag')}]: 空 direct 出站 → 补 domain_resolver={resolver}"
                "（否则启动时报 detour to an empty direct outbound）"
            )
        else:
            target["tcp_fast_open"] = True
            changes.append(
                f"outbounds[{target.get('tag')}]: 空 direct 出站 → 补 tcp_fast_open"
                "（配置里没有 IP 型 DNS server 可做 bootstrap）"
            )


def _migrate_rule_sets(rule_sets: Any, changes: list[str]) -> None:
    if not isinstance(rule_sets, list):
        return
    for entry in rule_sets:
        if isinstance(entry, dict) and "download_detour" in entry:
            detour = entry.pop("download_detour")
            entry["http_client"] = {"detour": detour}
            changes.append(f"rule_set[{entry.get('tag')}]: download_detour={detour} → http_client.detour")


def _migrate_inbounds(inbounds: Any, route_rules: list[Any], has_global_sniff: bool, changes: list[str]) -> tuple[list[Any], list[Any]]:
    """入站旧字段 → route 规则动作；返回 (sniff 规则, resolve 规则)。"""
    sniff_rules: list[Any] = []
    resolve_rules: list[Any] = []
    if not isinstance(inbounds, list):
        return sniff_rules, resolve_rules

    taken = {ib.get("tag") for ib in inbounds if isinstance(ib, dict) and ib.get("tag")}
    for inbound in inbounds:
        if not isinstance(inbound, dict):
            continue
        legacy = {k: inbound.pop(k) for k in list(inbound) if k in LEGACY_INBOUND_FIELDS}
        if not legacy:
            continue

        tag = inbound.get("tag")
        if not tag:  # 迁移后的规则要引用入站，没 tag 就补一个
            base = f"{inbound.get('type', 'in')}-in"
            tag, n = base, 2
            while tag in taken:
                tag = f"{base}{n}"
                n += 1
            inbound["tag"] = tag
            taken.add(tag)
            changes.append(f"inbounds: 无 tag 的 {inbound.get('type')} 入站补 tag={tag}")

        if legacy.get("sniff"):
            if has_global_sniff:
                changes.append(f"inbounds[{tag}]: sniff → 已有全局 action=sniff 规则，直接删除")
            else:
                rule: dict[str, Any] = {"inbound": tag, "action": "sniff"}
                if legacy.get("sniff_timeout"):
                    rule["timeout"] = legacy["sniff_timeout"]
                sniff_rules.append(rule)
                changes.append(f"inbounds[{tag}]: sniff → route 规则 action=sniff")

        if legacy.get("domain_strategy"):
            resolve_rules.append({"inbound": tag, "action": "resolve", "strategy": legacy["domain_strategy"]})
            changes.append(f"inbounds[{tag}]: domain_strategy={legacy['domain_strategy']} → route 规则 action=resolve")

        for field in ("sniff_override_destination", "udp_disable_domain_unmapping"):
            if field in legacy:
                changes.append(f"inbounds[{tag}]: 删除 {field}（新版已无对应字段）")

    return sniff_rules, resolve_rules


def migrate_config_with_changes(config: dict) -> tuple[dict, list[str]]:
    """迁移配置并返回 (新配置, 改动说明列表)。幂等：迁移过的配置再跑不会再变。"""
    if not isinstance(config, dict):
        raise ValueError("config 必须是一个 JSON 对象")

    out = copy.deepcopy(config)
    changes: list[str] = []
    had_route = isinstance(out.get("route"), dict)
    route: dict[str, Any] = out["route"] if had_route else {}

    # 1) route.rule_set 与旧 geosite/geoip 段
    rule_sets = route.get("rule_set")
    if not isinstance(rule_sets, list):
        rule_sets = []
    _migrate_rule_sets(rule_sets, changes)
    geosite_client = _download_client(route.pop("geosite", None))
    geoip_client = _download_client(route.pop("geoip", None))
    if geosite_client or geoip_client:
        changes.append("route: 移除旧 geosite/geoip 段，download_detour 并入 rule_set.http_client")

    # 2) dns.servers / dns.rules
    dns = out.get("dns")
    rcode_by_tag: dict[str, str] = {}
    dropped_resolver: str | None = None
    if isinstance(dns, dict):
        servers = dns.get("servers")
        if isinstance(servers, list):
            kept = []
            for server in servers:
                address = str(server.get("address", "")) if isinstance(server, dict) else ""
                if address.startswith("rcode://"):
                    tag, code = server.get("tag"), address.split("://", 1)[1].upper()
                    if tag:
                        rcode_by_tag[tag] = code
                    changes.append(f"dns.servers[{tag}]: {address!r} → 删除该 server，改由 DNS 规则 action=predefined 承担")
                    continue
                kept.append(_migrate_dns_server(server, changes))
            dns["servers"] = kept

        rules = dns.get("rules")
        if isinstance(rules, list):
            migrated_rules = []
            for rule in rules:
                if not isinstance(rule, dict):
                    migrated_rules.append(rule)
                    continue
                rule = dict(rule)
                if "outbound" in rule:  # 1.12 移除，等价物是 domain_resolver
                    dropped_resolver = rule.get("server") or dropped_resolver
                    changes.append(f"dns.rules: 移除旧 outbound 条件 {rule['outbound']!r}，改用 route.default_domain_resolver")
                    continue
                _migrate_legacy_conditions(rule, "geosite", rule_sets, geosite_client, changes)
                _migrate_legacy_conditions(rule, "geoip", rule_sets, geoip_client, changes)
                server = rule.get("server")
                if server in rcode_by_tag:
                    rule.pop("server", None)
                    rule["action"] = "predefined"
                    rule["rcode"] = rcode_by_tag[server]
                    changes.append(f"dns.rules: server={server} → action=predefined rcode={rcode_by_tag[server]}")
                elif server is not None and "action" not in rule:
                    rule["action"] = "route"
                    changes.append(f"dns.rules: server={server} 补 action=route")
                migrated_rules.append(rule)
            dns["rules"] = migrated_rules

    # 3) route.rules 里的旧 geosite/geoip 条件
    rules = route.get("rules")
    if not isinstance(rules, list):
        rules = []
    for rule in rules:
        if isinstance(rule, dict):
            _migrate_legacy_conditions(rule, "geosite", rule_sets, geosite_client, changes)
            _migrate_legacy_conditions(rule, "geoip", rule_sets, geoip_client, changes)

    # 4) inbounds 旧字段 → route 规则动作
    global_sniff = any(
        isinstance(r, dict) and r.get("action") == "sniff" and set(r) <= {"action", "sniffer", "timeout"}
        for r in rules
    )
    sniff_rules, resolve_rules = _migrate_inbounds(out.get("inbounds"), rules, global_sniff, changes)

    # 5) 顺序：sniff 必须早于 resolve
    if sniff_rules or resolve_rules:
        sniff_idx = [
            i for i, r in enumerate(rules)
            if isinstance(r, dict) and r.get("action") == "sniff" and set(r) <= {"action", "sniffer", "timeout"}
        ]
        rest = [r for i, r in enumerate(rules) if i not in set(sniff_idx)]
        rules = sniff_rules + [rules[i] for i in sniff_idx] + resolve_rules + rest
        if resolve_rules:
            changes.append("route.rules: 调整顺序，确保 sniff 动作排在 resolve 之前")

    if dropped_resolver and "default_domain_resolver" not in route:
        route["default_domain_resolver"] = dropped_resolver
        changes.append(f"route.default_domain_resolver = {dropped_resolver}")

    # 6) DNS server 的 detour 指向空 direct 出站（check 不报，run 才报）
    if isinstance(dns, dict):
        _fix_empty_direct_detours(out, dns, changes)

    if rule_sets:
        route["rule_set"] = rule_sets
    if rules:
        route["rules"] = rules
    if not had_route and route:
        out["route"] = route

    return out, changes


def migrate_config(config: dict) -> dict:
    """迁移配置，只返回新配置。"""
    return migrate_config_with_changes(config)[0]


# --------------------------------------------------------------------------- #
# 二、订阅拉取（按 User-Agent 协商格式 + 缓存，源站有限流）
# --------------------------------------------------------------------------- #

def _extract_ttl(headers: Any) -> float:
    """优先跟随源站 profile-update-interval（小时），并限制在合理区间。"""
    try:
        hours = float(str(headers.get("profile-update-interval", "")).strip() or 0)
    except ValueError:
        return CACHE_TTL_SECONDS
    if hours <= 0:
        return CACHE_TTL_SECONDS
    return max(CACHE_TTL_MIN, min(CACHE_TTL_MAX, hours * 3600.0))


def fetch_subscription(url: str, ua: str = DEFAULT_UA, use_cache: bool = True) -> dict:
    """按 sing-box 的 User-Agent 拉取订阅并解析成配置对象。"""
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise ValueError("url 必须是 http(s) 订阅地址")

    key = f"{ua}\n{url}"
    if use_cache:
        hit = _cache.get(key)
        if hit and time.time() - hit[0] < hit[2]:
            return copy.deepcopy(hit[1])

    request = urllib.request.Request(
        url,
        headers={"User-Agent": ua, "Accept": "application/json, */*", "Accept-Encoding": "identity"},
    )
    try:
        with urllib.request.urlopen(request, timeout=FETCH_TIMEOUT_SECONDS) as response:
            raw = response.read()
            ttl = _extract_ttl(response.headers)
    except urllib.error.HTTPError as exc:
        raise ValueError(f"订阅返回 HTTP {exc.code}：{exc.reason}") from exc
    except Exception as exc:
        raise ValueError(f"拉取订阅失败（源站可能限流或该 UA 不被接受）：{exc}") from exc

    text = raw.decode("utf-8", errors="replace").strip()
    try:
        config = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError(
            "订阅返回的不是 sing-box JSON 配置；该源可能按 User-Agent 下发 base64 节点列表或 Clash YAML，"
            "请改用 sing-box 客户端的 User-Agent（参数 ua）"
        ) from exc
    if not isinstance(config, dict):
        raise ValueError("订阅返回的 JSON 顶层不是对象，不是 sing-box 配置")

    if use_cache:
        _cache[key] = (time.time(), copy.deepcopy(config), ttl)
    return config


# --------------------------------------------------------------------------- #
# 三、两个 convertor
# --------------------------------------------------------------------------- #

@convertor(
    method="POST",
    category="singbox",
    produces="application/json",
    example_input={"config": {"dns": {"servers": [{"address": "1.1.1.1", "tag": "remote"}]}}},
    example_output={"dns": {"servers": [{"type": "udp", "tag": "remote", "server": "1.1.1.1"}]}},
)
def singbox_migrate(config: dict) -> dict:
    """迁移一份 sing-box 配置：legacy DNS server、rule_set 下载字段、入站旧字段全部改到 1.14+ 新格式。"""
    return migrate_config(config)


@convertor(
    method="GET",
    category="singbox",
    produces="application/json",
    example_input={"url": "https://example.com/api/v1/client/subscribe?token=TOKEN", "ua": DEFAULT_UA},
    example_output={"outbounds": [{"type": "trojan", "tag": "node-01"}]},
)
def singbox_fix(url: str, ua: str = DEFAULT_UA) -> dict:
    """拉取一个订阅（按 sing-box UA 取 JSON 配置）并返回迁移后的配置，可直接当订阅地址用。"""
    return migrate_config(fetch_subscription(url, ua))
