"""代理订阅加载与解析。

支持三类输入：
  - 纯文本 HTTP/SOCKS 代理，每行一个
  - base64 包裹的纯文本代理
  - 常见 Clash YAML 中 type=http/https/socks/socks5/socks5h 的直连代理节点

说明：注册流程里的 curl_cffi 需要可直接作为 proxy 使用的 URL，格式如：
  socks5://user:pass@host:1080
  http://host:8080
"""
from __future__ import annotations

import base64
import re
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import quote, urlparse, urlunparse

import requests

DIRECT_SCHEMES = {"http", "https", "socks4", "socks5", "socks5h"}
UNSUPPORTED_SCHEMES = {
    "ss", "ssr", "vmess", "vless", "trojan", "tuic", "hysteria", "hysteria2", "hy2", "juicity",
}
CLASH_DIRECT_TYPES = {"http", "https", "socks", "socks5", "socks5h"}
CLASH_UNSUPPORTED_TYPES = {
    "ss", "ssr", "vmess", "vless", "trojan", "tuic", "hysteria", "hysteria2", "hy2", "snell", "wireguard",
}


@dataclass
class ProxySubscriptionResult:
    ok: bool
    proxies: list[str] = field(default_factory=list)
    unsupported_count: int = 0
    warnings: list[str] = field(default_factory=list)
    source: str = ""
    status_code: int | None = None
    content_type: str = ""
    error: str = ""

    def to_dict(self, include_proxies: bool = True) -> dict[str, Any]:
        out = {
            "ok": self.ok,
            "count": len(self.proxies),
            "unsupported_count": self.unsupported_count,
            "warnings": self.warnings,
            "source": self.source,
            "status_code": self.status_code,
            "content_type": self.content_type,
            "error": self.error,
        }
        if include_proxies:
            out["proxies"] = self.proxies
        return out


def mask_url(url: str) -> str:
    """隐藏订阅 token，日志/UI 状态只展示域名与参数概览。"""
    if not url:
        return ""
    try:
        p = urlparse(url)
        path = p.path or ""
        parts = [x for x in path.split("/") if x]
        if parts:
            tail = parts[-1]
            if len(tail) > 10:
                parts[-1] = f"{tail[:4]}…{tail[-4:]}"
            path = "/" + "/".join(parts)
        query = "…" if p.query else ""
        return urlunparse((p.scheme, p.netloc, path, "", query, ""))
    except Exception:
        return url[:16] + "…" if len(url) > 16 else url


def dedupe_preserve_order(items: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for item in items:
        s = (item or "").strip()
        if not s or s in seen:
            continue
        seen.add(s)
        out.append(s)
    return out


def load_proxy_subscription(url: str, timeout: int = 15) -> ProxySubscriptionResult:
    url = (url or "").strip()
    if not url:
        return ProxySubscriptionResult(ok=False, error="订阅地址为空")
    try:
        resp = requests.get(
            url,
            timeout=max(3, int(timeout or 15)),
            headers={
                "User-Agent": "ClashforWindows/0.20.39",
                "Accept": "text/plain, application/yaml, application/x-yaml, */*",
            },
        )
        text = resp.text or ""
        result = parse_proxy_subscription(text)
        result.ok = 200 <= resp.status_code < 300
        result.status_code = resp.status_code
        result.content_type = resp.headers.get("content-type", "")
        result.source = mask_url(url)
        if not result.ok:
            result.error = f"HTTP {resp.status_code}"
        return result
    except Exception as e:
        return ProxySubscriptionResult(ok=False, source=mask_url(url), error=str(e))


def parse_proxy_subscription(text: str) -> ProxySubscriptionResult:
    raw = (text or "").strip().lstrip("\ufeff")
    if not raw:
        return ProxySubscriptionResult(ok=True, warnings=["订阅内容为空"])

    variants = [("raw", raw)]
    decoded = _maybe_base64_decode(raw)
    if decoded and decoded != raw:
        variants.insert(0, ("base64", decoded))

    best: ProxySubscriptionResult | None = None
    for kind, body in variants:
        proxies, unsupported, warnings = _parse_any(body)
        res = ProxySubscriptionResult(
            ok=True,
            proxies=dedupe_preserve_order(proxies),
            unsupported_count=unsupported,
            warnings=warnings,
            source=kind,
        )
        if best is None or len(res.proxies) > len(best.proxies):
            best = res

    assert best is not None
    if best.unsupported_count and not best.proxies:
        best.warnings.append(
            "解析到 Clash/通用订阅节点，但没有 HTTP/SOCKS 直连代理；可使用本地 Clash mixed-port，例如 socks5://127.0.0.1:7897。"
        )
    elif best.unsupported_count:
        best.warnings.append(f"已跳过 {best.unsupported_count} 个非 HTTP/SOCKS 节点")
    return best


def _parse_any(text: str) -> tuple[list[str], int, list[str]]:
    direct, unsupported_a = _parse_direct_lines(text)
    clash, unsupported_b = _parse_clash_yaml(text)
    warnings: list[str] = []
    return direct + clash, unsupported_a + unsupported_b, warnings


def _maybe_base64_decode(text: str) -> str:
    compact = "".join(text.split())
    if len(compact) < 16:
        return ""
    if not re.fullmatch(r"[A-Za-z0-9+/=_-]+", compact):
        return ""
    padded = compact.replace("-", "+").replace("_", "/")
    padded += "=" * ((4 - len(padded) % 4) % 4)
    try:
        data = base64.b64decode(padded, validate=False)
        decoded = data.decode("utf-8", "ignore").strip()
        # 至少看起来像代理/订阅内容
        lower = decoded.lower()
        if any(x in lower for x in ("://", "proxies:", "server:", "port:")):
            return decoded
    except Exception:
        return ""
    return ""


def _parse_direct_lines(text: str) -> tuple[list[str], int]:
    proxies: list[str] = []
    unsupported = 0
    for raw_line in text.splitlines():
        line = raw_line.strip().strip('"\'')
        if not line or line.startswith("#") or line.startswith("//"):
            continue
        # 有些订阅一行里包含多个 URI，用空白切开
        for part in re.split(r"\s+", line):
            part = part.strip().strip(",").strip('"\'')
            m = re.match(r"^([a-zA-Z][a-zA-Z0-9+.-]*)://", part)
            if not m:
                continue
            scheme = m.group(1).lower()
            if scheme in DIRECT_SCHEMES:
                proxies.append(part)
            elif scheme in UNSUPPORTED_SCHEMES:
                unsupported += 1
    return proxies, unsupported


def _parse_clash_yaml(text: str) -> tuple[list[str], int]:
    proxies: list[str] = []
    unsupported = 0
    lines = text.splitlines()
    in_proxies = False
    current: dict[str, str] | None = None
    current_indent = 0

    def flush():
        nonlocal current, unsupported
        if not current:
            return
        url, is_unsupported = _clash_proxy_to_url(current)
        if url:
            proxies.append(url)
        elif is_unsupported:
            unsupported += 1
        current = None

    for raw in lines:
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        indent = len(raw) - len(raw.lstrip(" "))
        line = raw.strip()
        if re.match(r"^proxies\s*:\s*$", line):
            in_proxies = True
            current = None
            current_indent = indent
            continue
        if in_proxies and indent <= current_indent and not line.startswith("-"):
            flush()
            in_proxies = False
        if not in_proxies:
            continue
        if line.startswith("-"):
            flush()
            payload = line[1:].strip()
            current = {}
            if payload.startswith("{") and payload.endswith("}"):
                current.update(_parse_inline_map(payload))
                flush()
            elif payload:
                # 形如 - name: xxx
                k, v = _split_yaml_kv(payload)
                if k:
                    current[k] = v
        elif current is not None:
            k, v = _split_yaml_kv(line)
            if k:
                current[k] = v
    flush()
    return proxies, unsupported


def _parse_inline_map(text: str) -> dict[str, str]:
    body = text.strip().strip("{}").strip()
    out: dict[str, str] = {}
    for part in _split_csv_like(body):
        k, v = _split_yaml_kv(part)
        if k:
            out[k] = v
    return out


def _split_csv_like(text: str) -> list[str]:
    parts: list[str] = []
    buf: list[str] = []
    quote_char = ""
    escape = False
    for ch in text:
        if escape:
            buf.append(ch)
            escape = False
            continue
        if ch == "\\":
            buf.append(ch)
            escape = True
            continue
        if quote_char:
            buf.append(ch)
            if ch == quote_char:
                quote_char = ""
            continue
        if ch in ('"', "'"):
            quote_char = ch
            buf.append(ch)
            continue
        if ch == ",":
            parts.append("".join(buf).strip())
            buf = []
        else:
            buf.append(ch)
    if buf:
        parts.append("".join(buf).strip())
    return parts


def _split_yaml_kv(text: str) -> tuple[str, str]:
    if ":" not in text:
        return "", ""
    k, v = text.split(":", 1)
    k = k.strip().strip('"\'')
    v = v.strip().strip(",").strip()
    if (v.startswith('"') and v.endswith('"')) or (v.startswith("'") and v.endswith("'")):
        v = v[1:-1]
    return k, v


def _clash_proxy_to_url(item: dict[str, str]) -> tuple[str, bool]:
    typ = (item.get("type") or "").strip().lower()
    if typ in CLASH_UNSUPPORTED_TYPES:
        return "", True
    if typ not in CLASH_DIRECT_TYPES:
        return "", False
    server = (item.get("server") or item.get("host") or "").strip()
    port = str(item.get("port") or "").strip().strip('"\'')
    if not server or not port:
        return "", False
    scheme = "socks5" if typ in {"socks", "socks5", "socks5h"} else ("https" if typ == "https" else "http")
    username = (item.get("username") or item.get("user") or "").strip()
    password = (item.get("password") or item.get("pass") or "").strip()
    auth = ""
    if username or password:
        auth = quote(username, safe="")
        if password:
            auth += ":" + quote(password, safe="")
        auth += "@"
    return f"{scheme}://{auth}{server}:{port}", False
