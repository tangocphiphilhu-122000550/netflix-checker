"""Netflix cookie file parsing (Netscape, JSON, raw key=value)."""

from __future__ import annotations

import json
import re
from typing import Any, Dict, List, Optional

LOGIN_REQUIRED = ("NetflixId",)
OPTIONAL = ("SecureNetflixId", "nfvdid", "OptanonConsent")
ALL_NAMES = set(LOGIN_REQUIRED + OPTIONAL)
CANONICAL = {name.lower(): name for name in ALL_NAMES}


def is_netflix_domain(domain: Any) -> bool:
    normalized = str(domain or "").strip()
    if normalized.startswith("#HttpOnly_"):
        normalized = normalized[len("#HttpOnly_") :]
    return "netflix." in normalized.lower()


def canonicalize_name(name: Any) -> str:
    normalized = str(name or "").strip()
    return CANONICAL.get(normalized.lower(), normalized)


def is_netflix_cookie_entry(domain: Any, name: Any) -> bool:
    normalized = canonicalize_name(name)
    return normalized in ALL_NAMES or is_netflix_domain(domain)


def has_required_cookies(cookie_dict: Dict[str, str]) -> bool:
    if not isinstance(cookie_dict, dict):
        return False
    for name in LOGIN_REQUIRED:
        if not str(cookie_dict.get(name) or "").strip():
            return False
    return True


def split_netscape_columns(line: str) -> List[str]:
    stripped = line.strip()
    if not stripped:
        return []
    if stripped.startswith("#") and not stripped.startswith("#HttpOnly_"):
        return []
    if stripped.startswith("#HttpOnly_"):
        stripped = stripped[len("#HttpOnly_") :]
    if not stripped:
        return []
    parts = stripped.split("\t")
    if len(parts) >= 7:
        return parts[:6] + ["\t".join(parts[6:])]
    parts = re.split(r"\s+", stripped, maxsplit=6)
    return parts if len(parts) >= 7 else []


def is_netscape_line(line: str) -> bool:
    parts = split_netscape_columns(line)
    if len(parts) < 7:
        return False
    if parts[1].upper() not in ("TRUE", "FALSE"):
        return False
    if parts[3].upper() not in ("TRUE", "FALSE"):
        return False
    if not re.match(r"^-?\d+(?:\.\d+)?$", parts[4].strip()):
        return False
    return True


def build_entry(
    domain: Any,
    tail_match: Any,
    path: Any,
    secure: Any,
    expires: Any,
    name: Any,
    value: Any,
    position: int,
) -> Dict[str, Any]:
    expires_str = str(expires or 0).strip()
    if re.fullmatch(r"-?\d+\.\d+", expires_str):
        try:
            expires_str = str(int(float(expires_str)))
        except Exception:
            pass
    return {
        "domain": str(domain or "").replace("#HttpOnly_", "", 1),
        "tail_match": "TRUE" if str(tail_match).upper() == "TRUE" else "FALSE",
        "path": str(path or "/"),
        "secure": "TRUE" if str(secure).upper() == "TRUE" else "FALSE",
        "expires": expires_str or "0",
        "name": canonicalize_name(name),
        "value": str(value or ""),
        "position": position,
    }


def format_entry(entry: Dict[str, Any]) -> str:
    return (
        f"{entry['domain']}\t{entry['tail_match']}\t{entry['path']}\t{entry['secure']}\t"
        f"{entry['expires']}\t{entry['name']}\t{entry['value']}"
    )


def extract_netscape_entries(raw_text: str) -> List[Dict[str, Any]]:
    entries: List[Dict[str, Any]] = []
    for index, line in enumerate(raw_text.splitlines()):
        if not is_netscape_line(line):
            continue
        parts = split_netscape_columns(line)
        if len(parts) < 7:
            continue
        domain, name = parts[0], canonicalize_name(parts[5])
        if not is_netflix_cookie_entry(domain, name):
            continue
        entries.append(
            build_entry(domain, parts[1], parts[2], parts[3], parts[4], name, parts[6], index)
        )
    return entries


def extract_json_entries(content: str) -> List[Dict[str, Any]]:
    try:
        data = json.loads(content)
    except Exception:
        return []
    if isinstance(data, dict):
        if isinstance(data.get("cookies"), list):
            data = data["cookies"]
        elif isinstance(data.get("items"), list):
            data = data["items"]
        else:
            data = [data]
    if not isinstance(data, list):
        return []
    entries: List[Dict[str, Any]] = []
    for index, cookie in enumerate(data):
        if not isinstance(cookie, dict):
            continue
        domain = cookie.get("domain", "")
        name = canonicalize_name(cookie.get("name", ""))
        if not is_netflix_cookie_entry(domain, name):
            continue
        entries.append(
            build_entry(
                domain,
                "TRUE" if str(domain).startswith(".") else "FALSE",
                cookie.get("path", "/"),
                "TRUE" if cookie.get("secure", False) else "FALSE",
                cookie.get("expirationDate", cookie.get("expiration", 0)),
                name,
                cookie.get("value", ""),
                index,
            )
        )
    return entries


def extract_raw_entries(raw_text: str) -> List[Dict[str, Any]]:
    pattern = re.compile(
        rf"(?:['\"])?(?P<name>{'|'.join(sorted((re.escape(n) for n in ALL_NAMES), key=len, reverse=True))})"
        r"(?:['\"])?\s*(?:=|:)\s*(?P<value>\"[^\"]*\"|'[^']*'|[^;\s]+)",
        re.IGNORECASE,
    )
    entries: List[Dict[str, Any]] = []
    for index, match in enumerate(pattern.finditer(raw_text)):
        cookie_name = canonicalize_name(match.group("name"))
        value = match.group("value")
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        else:
            value = value.rstrip(",")
        entries.append(
            build_entry(
                ".netflix.com",
                "TRUE",
                "/",
                "TRUE" if cookie_name == "SecureNetflixId" else "FALSE",
                "0",
                cookie_name,
                value,
                index,
            )
        )
    return entries


def cookies_dict_from_netscape(netscape_text: str) -> Dict[str, str]:
    cookies: Dict[str, str] = {}
    for line in netscape_text.splitlines():
        parts = split_netscape_columns(line)
        if len(parts) >= 7:
            domain = parts[0]
            name = canonicalize_name(parts[5])
            if is_netflix_cookie_entry(domain, name):
                cookies[name] = parts[6]
    return cookies


def build_bundles_from_entries(entries: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    if not entries:
        return []
    by_name: Dict[str, List[Dict[str, Any]]] = {}
    for entry in entries:
        name = entry.get("name")
        if not name:
            continue
        by_name.setdefault(name, []).append(entry)
    if not by_name:
        return []

    netflix_id_count = len(by_name.get("NetflixId", []))
    bundle_count = netflix_id_count or max(len(v) for v in by_name.values())
    bundles: List[Dict[str, Any]] = []

    for bundle_index in range(bundle_count):
        selected: List[Dict[str, Any]] = []
        for name_entries in by_name.values():
            if bundle_index < len(name_entries):
                selected.append(name_entries[bundle_index])
            elif len(name_entries) == 1:
                selected.append(name_entries[0])
        if not selected:
            continue
        selected = sorted(selected, key=lambda item: item.get("position", 0))
        netscape_text = "\n".join(format_entry(e) for e in selected)
        cookies = cookies_dict_from_netscape(netscape_text)
        if not has_required_cookies(cookies):
            continue
        bundles.append(
            {
                "index": bundle_index + 1,
                "total": bundle_count,
                "netscape_text": netscape_text,
                "cookies": cookies,
            }
        )
    return bundles


def extract_cookie_bundles(content: str) -> List[Dict[str, Any]]:
    for extractor in (extract_json_entries, extract_netscape_entries, extract_raw_entries):
        bundles = build_bundles_from_entries(extractor(content))
        if bundles:
            return bundles
    return []


def apply_cookies_to_session(session: Any, cookies: Dict[str, str]) -> None:
    for name, value in cookies.items():
        if not value:
            continue
        session.cookies.set(name, value, domain=".netflix.com", path="/")
