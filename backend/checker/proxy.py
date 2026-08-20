"""Proxy file parsing and rotation helpers."""

from __future__ import annotations

import os
import re
from typing import Any, Dict, List, Optional, Set, Tuple

PROXY_FILE = "proxy.txt"


def _build_proxy_dict(
    scheme: str,
    host: str,
    port: str,
    user: Optional[str] = None,
    password: Optional[str] = None,
) -> Dict[str, str]:
    host = host.strip()
    if host.startswith("[") and host.endswith("]"):
        host = host[1:-1]
    if user is not None and password is not None:
        proxy_url = f"{scheme}://{user}:{password}@{host}:{port}"
    else:
        proxy_url = f"{scheme}://{host}:{port}"
    return {"http": proxy_url, "https": proxy_url}


def parse_proxy_line(line: str) -> Optional[Dict[str, str]]:
    line = line.strip()
    if not line or line.startswith("#"):
        return None

    line = re.sub(r"^([a-zA-Z][a-zA-Z0-9+.-]*):/+", r"\1://", line)
    line = re.sub(r"\s+", " ", line).strip()

    url_like = re.match(
        r"^(?P<scheme>https?|socks5h?|socks4a?)://"
        r"(?:(?P<user>[^:@\s]+):(?P<password>[^@\s]+)@)?"
        r"(?P<host>\[[^\]]+\]|[^:\s]+):(?P<port>\d+)$",
        line,
        flags=re.IGNORECASE,
    )
    if url_like:
        data = url_like.groupdict()
        return _build_proxy_dict(
            data["scheme"].lower(),
            data["host"],
            data["port"],
            data.get("user"),
            data.get("password"),
        )

    userpass_hostport = re.match(
        r"^(?P<user>[^:@\s]+):(?P<password>[^@\s]+)@"
        r"(?P<host>\[[^\]]+\]|[^:\s]+):(?P<port>\d+)$",
        line,
    )
    if userpass_hostport:
        d = userpass_hostport.groupdict()
        return _build_proxy_dict("http", d["host"], d["port"], d["user"], d["password"])

    hostport_userpass = re.match(
        r"^(?P<host>\[[^\]]+\]|[^:\s]+):(?P<port>\d+)@"
        r"(?P<user>[^:@\s]+):(?P<password>[^@\s]+)$",
        line,
    )
    if hostport_userpass:
        d = hostport_userpass.groupdict()
        return _build_proxy_dict("http", d["host"], d["port"], d["user"], d["password"])

    hostport = re.match(r"^(?P<host>\[[^\]]+\]|[^:\s]+):(?P<port>\d+)$", line)
    if hostport:
        d = hostport.groupdict()
        return _build_proxy_dict("http", d["host"], d["port"])

    parts = line.split(":")
    if len(parts) == 4:
        a, b, c, d = parts
        if b.isdigit() and not d.isdigit():
            return _build_proxy_dict("http", a, b, c, d)
        if d.isdigit() and not b.isdigit():
            return _build_proxy_dict("http", c, d, a, b)

    for pattern in (
        r"^(?P<host>\[[^\]]+\]|[^:\s]+):(?P<port>\d+)\s+(?P<user>[^:\s]+):(?P<password>\S+)$",
        r"^(?P<host>\[[^\]]+\]|[^:\s]+):(?P<port>\d+)\|(?P<user>[^:\s]+):(?P<password>\S+)$",
        r"^(?P<host>\[[^\]]+\]|[^:\s]+):(?P<port>\d+);(?P<user>[^:\s]+):(?P<password>\S+)$",
        r"^(?P<host>\[[^\]]+\]|[^:\s]+):(?P<port>\d+),(?P<user>[^:\s]+):(?P<password>\S+)$",
    ):
        match = re.match(pattern, line)
        if match:
            d = match.groupdict()
            return _build_proxy_dict("http", d["host"], d["port"], d["user"], d["password"])

    return None


def load_proxies(path: str = PROXY_FILE) -> List[Dict[str, str]]:
    proxies: List[Dict[str, str]] = []
    if not os.path.exists(path):
        return proxies
    with open(path, "r", encoding="utf-8") as fh:
        for line in fh:
            proxy = parse_proxy_line(line)
            if proxy:
                proxies.append(proxy)
    return proxies


def pick_proxy(
    proxies: List[Dict[str, str]],
    used: Set[int],
) -> Tuple[Optional[Dict[str, str]], Optional[int]]:
    if not proxies:
        return None, None
    available = [i for i in range(len(proxies)) if i not in used]
    if not available:
        used.clear()
        available = list(range(len(proxies)))
    idx = available[0]
    return proxies[idx], idx
