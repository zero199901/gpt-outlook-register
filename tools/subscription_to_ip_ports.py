#!/usr/bin/env python3
"""Fetch a Clash subscription and extract node server/ip/port rows.

输出说明：
- server:port 是订阅节点入口；ss/vmess/trojan 等需要 Clash/Mihomo/Xray/Sing-box 转成本地 SOCKS/HTTP 端口后给应用使用。
- --resolve 会把 server 域名解析为 IP，输出 ip:port。
"""
from __future__ import annotations

import argparse
import base64
import csv
import json
import re
import socket
import sys
from dataclasses import dataclass, asdict
from typing import Iterable

import requests


@dataclass
class NodeEndpoint:
    name: str
    type: str
    server: str
    port: int | None
    ip: str = ""

    @property
    def server_port(self) -> str:
        return f"{self.server}:{self.port}" if self.server and self.port else ""

    @property
    def ip_port(self) -> str:
        return f"{self.ip}:{self.port}" if self.ip and self.port else ""


def fetch_subscription(url: str, timeout: int = 30) -> str:
    resp = requests.get(
        url,
        timeout=timeout,
        headers={
            "User-Agent": "ClashforWindows/0.20.39",
            "Accept": "text/plain, application/yaml, application/x-yaml, */*",
        },
    )
    resp.raise_for_status()
    text = resp.text or ""
    decoded = maybe_base64_decode(text)
    return decoded or text


def maybe_base64_decode(text: str) -> str:
    compact = "".join((text or "").strip().split())
    if len(compact) < 16:
        return ""
    if not re.fullmatch(r"[A-Za-z0-9+/=_-]+", compact):
        return ""
    padded = compact.replace("-", "+").replace("_", "/")
    padded += "=" * ((4 - len(padded) % 4) % 4)
    try:
        data = base64.b64decode(padded, validate=False)
        decoded = data.decode("utf-8", "ignore").strip()
        lower = decoded.lower()
        if any(x in lower for x in ("://", "proxies:", "server:", "port:")):
            return decoded
    except Exception:
        return ""
    return ""


def split_csv_like(body: str) -> list[str]:
    parts: list[str] = []
    cur: list[str] = []
    quote = ""
    esc = False
    for ch in body:
        if esc:
            cur.append(ch)
            esc = False
            continue
        if ch == "\\":
            cur.append(ch)
            esc = True
            continue
        if quote:
            cur.append(ch)
            if ch == quote:
                quote = ""
            continue
        if ch in ('"', "'"):
            quote = ch
            cur.append(ch)
            continue
        if ch == ",":
            parts.append("".join(cur).strip())
            cur = []
            continue
        cur.append(ch)
    if cur:
        parts.append("".join(cur).strip())
    return parts


def split_kv(text: str) -> tuple[str, str]:
    if ":" not in text:
        return "", ""
    k, v = text.split(":", 1)
    return k.strip().strip('"\''), v.strip().strip('"\'')


def parse_inline_map(text: str) -> dict[str, str]:
    body = text.strip().strip("{}").strip()
    out: dict[str, str] = {}
    for part in split_csv_like(body):
        k, v = split_kv(part)
        if k:
            out[k] = v
    return out


def parse_clash_nodes(text: str) -> list[NodeEndpoint]:
    nodes: list[NodeEndpoint] = []
    in_proxies = False
    current: dict[str, str] | None = None
    current_indent = 0

    def flush() -> None:
        nonlocal current
        if not current:
            return
        name = str(current.get("name") or "").strip()
        typ = str(current.get("type") or "").strip().lower()
        server = str(current.get("server") or "").strip()
        port_s = str(current.get("port") or "").strip()
        try:
            port = int(port_s)
        except Exception:
            port = None
        if server and port:
            nodes.append(NodeEndpoint(name=name, type=typ, server=server, port=port))
        current = None

    for raw in text.splitlines():
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
                current.update(parse_inline_map(payload))
                flush()
            elif payload:
                k, v = split_kv(payload)
                if k:
                    current[k] = v
        elif current is not None:
            k, v = split_kv(line)
            if k:
                current[k] = v
    flush()
    return nodes


def resolve_ip(server: str, timeout: float = 3.0) -> str:
    old_timeout = socket.getdefaulttimeout()
    socket.setdefaulttimeout(timeout)
    try:
        infos = socket.getaddrinfo(server, None, type=socket.SOCK_STREAM)
        for info in infos:
            ip = info[4][0]
            if ip:
                return ip
    except Exception:
        return ""
    finally:
        socket.setdefaulttimeout(old_timeout)
    return ""


def output_nodes(nodes: list[NodeEndpoint], fmt: str) -> None:
    if fmt == "json":
        print(json.dumps([asdict(n) | {"server_port": n.server_port, "ip_port": n.ip_port} for n in nodes], ensure_ascii=False, indent=2))
        return
    if fmt == "csv":
        w = csv.DictWriter(sys.stdout, fieldnames=["name", "type", "server", "port", "ip", "server_port", "ip_port"])
        w.writeheader()
        for n in nodes:
            w.writerow({**asdict(n), "server_port": n.server_port, "ip_port": n.ip_port})
        return
    for n in nodes:
        print(n.ip_port or n.server_port)


def main() -> None:
    ap = argparse.ArgumentParser(description="Fetch Clash subscription and output node server/ip:port")
    ap.add_argument("url")
    ap.add_argument("--resolve", action="store_true", help="resolve server domains to IP addresses")
    ap.add_argument("--format", choices=["plain", "csv", "json"], default="csv")
    ap.add_argument("--type", default="", help="filter node type, e.g. ss,vmess")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--timeout", type=int, default=30)
    args = ap.parse_args()

    text = fetch_subscription(args.url, timeout=args.timeout)
    nodes = parse_clash_nodes(text)
    if args.type:
        allow = {x.strip().lower() for x in args.type.split(",") if x.strip()}
        nodes = [n for n in nodes if n.type in allow]
    if args.limit > 0:
        nodes = nodes[: args.limit]
    if args.resolve:
        for n in nodes:
            n.ip = resolve_ip(n.server)
    output_nodes(nodes, args.format)


if __name__ == "__main__":
    main()
