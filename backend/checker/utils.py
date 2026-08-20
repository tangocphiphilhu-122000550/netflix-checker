"""Shared helpers: decoding, booleans, dates, phone, console."""

from __future__ import annotations

import html
import os
import re
import sys
import unicodedata
from datetime import datetime
from typing import Any, Iterable, Optional


def clear_screen() -> None:
    os.system("cls" if os.name == "nt" else "clear")


def set_console_title(title: str) -> None:
    if os.name == "nt":
        os.system(f"title NetflixChecker - {title}")
    else:
        sys.stdout.write(f"\033]0;{title}\007")
        sys.stdout.flush()


def color_text(text: str, code: str, enabled: bool = True) -> str:
    if not enabled:
        return text
    return f"{code}{text}\033[0m"


def _decode_unicode_escape(match: re.Match) -> str:
    try:
        return chr(int(match.group(1), 16))
    except Exception:
        return match.group(0)


def decode_netflix_value(value: Any) -> Optional[str]:
    if value is None:
        return None
    cleaned = html.unescape(str(value))
    for source, target in {
        "\\x20": " ",
        "\\u00A0": " ",
        "\\u00a0": " ",
        "&nbsp;": " ",
        "u00A0": " ",
    }.items():
        cleaned = cleaned.replace(source, target)
    cleaned = (
        cleaned.replace("\\/", "/")
        .replace('\\"', '"')
        .replace("\\n", " ")
        .replace("\\t", " ")
    )
    for _ in range(3):
        previous = cleaned
        cleaned = re.sub(r"\\u([0-9a-fA-F]{4})", _decode_unicode_escape, cleaned)
        cleaned = re.sub(r"\\x([0-9a-fA-F]{2})", _decode_unicode_escape, cleaned)
        cleaned = re.sub(
            r"(?<!\\)\bu([0-9a-fA-F]{4})(?![0-9a-fA-F])",
            _decode_unicode_escape,
            cleaned,
        )
        cleaned = cleaned.replace("\\\\", "\\")
        if cleaned == previous:
            break
    cleaned = re.sub(r"(?<=[A-Za-z])\s+(?=[^\x00-\x7F])", "", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return cleaned or None


def parse_boolean_value(value: Any) -> Optional[bool]:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if value == 1:
            return True
        if value == 0:
            return False
        return None
    if isinstance(value, dict):
        for key in (
            "value",
            "isUserOnHold",
            "holdStatus",
            "isOnHold",
            "pastDue",
            "isPastDue",
            "isVerified",
            "verified",
        ):
            if key in value:
                parsed = parse_boolean_value(value.get(key))
                if parsed is not None:
                    return parsed
        return None
    cleaned = decode_netflix_value(value)
    if cleaned is None:
        return None
    lowered = str(cleaned).strip().lower()
    if lowered in {"true", "yes", "1", "on"}:
        return True
    if lowered in {"false", "no", "0", "off"}:
        return False
    return None


def format_boolean_label(value: Any) -> Optional[str]:
    parsed = parse_boolean_value(value)
    if parsed is True:
        return "Yes"
    if parsed is False:
        return "No"
    return None


def normalize_plan_key(plan_name: Optional[str]) -> str:
    if not plan_name:
        return "unknown"
    simplified = unicodedata.normalize("NFKD", plan_name)
    simplified = "".join(ch for ch in simplified if not unicodedata.combining(ch))
    normalized = re.sub(r"[^\w]+", "_", simplified.lower(), flags=re.UNICODE).strip("_")
    return normalized or "unknown"


def format_plan_label(plan_key: Optional[str]) -> str:
    if not plan_key:
        return "Unknown"
    label = plan_key.replace("_", " ").strip()
    return label.title() if label else "Unknown"


def int_or_none(value: Any) -> Optional[int]:
    cleaned = decode_netflix_value(value)
    if cleaned is None:
        return None
    try:
        return int(str(cleaned).strip())
    except Exception:
        match = re.search(r"\d+", str(cleaned))
        if match:
            try:
                return int(match.group(0))
            except Exception:
                return None
        return None


def normalize_output_value(
    value: Any,
    unknown_fallback: str = "UNKNOWN",
    na_when_false: bool = False,
) -> str:
    cleaned = decode_netflix_value(value)
    if cleaned is None:
        return unknown_fallback
    if na_when_false and cleaned.lower() in {"false", "no", "0", "off", "n/a", "na"}:
        return "N/A"
    if cleaned.lower() in {"null", "none", "n/a"}:
        return unknown_fallback
    return cleaned


MONTH_ALIASES = {
    "january": 1, "jan": 1, "janvier": 1, "enero": 1, "janeiro": 1, "thang 1": 1,
    "february": 2, "feb": 2, "fevrier": 2, "febrero": 2, "fevereiro": 2, "thang 2": 2,
    "march": 3, "mar": 3, "mars": 3, "marzo": 3, "marco": 3, "thang 3": 3,
    "april": 4, "apr": 4, "avril": 4, "abril": 4, "thang 4": 4,
    "may": 5, "mai": 5, "mayo": 5, "thang 5": 5,
    "june": 6, "jun": 6, "juin": 6, "junio": 6, "junho": 6, "thang 6": 6,
    "july": 7, "jul": 7, "juillet": 7, "julio": 7, "julho": 7, "thang 7": 7,
    "august": 8, "aug": 8, "aout": 8, "agosto": 8, "thang 8": 8,
    "september": 9, "sep": 9, "septembre": 9, "septiembre": 9, "setembro": 9, "thang 9": 9,
    "october": 10, "oct": 10, "octobre": 10, "octubre": 10, "outubro": 10, "thang 10": 10,
    "november": 11, "nov": 11, "novembre": 11, "noviembre": 11, "novembro": 11, "thang 11": 11,
    "december": 12, "dec": 12, "decembre": 12, "diciembre": 12, "dezembro": 12, "thang 12": 12,
}


def normalize_calendar_year(year: Any) -> Optional[int]:
    try:
        year = int(year)
    except Exception:
        return None
    if 2400 <= year <= 2700:
        return year - 543
    return year


def parse_localized_date(cleaned: str) -> Optional[datetime]:
    if not cleaned:
        return None
    for parser in (
        "%Y-%m-%d",
        "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%dT%H:%M:%S.%f",
        "%Y-%m-%dT%H:%M:%S.%f%z",
    ):
        try:
            return datetime.strptime(cleaned, parser)
        except Exception:
            continue
    try:
        return datetime.fromisoformat(cleaned.replace("Z", "+00:00"))
    except Exception:
        pass

    east_asian = re.search(
        r"(?P<year>\d{4})\s*[年년]\s*(?P<month>\d{1,2})\s*[月월]"
        r"(?:\s*(?P<day>\d{1,2})\s*[日일])?",
        cleaned,
    )
    if east_asian:
        try:
            year = normalize_calendar_year(east_asian.group("year"))
            month = int(east_asian.group("month"))
            day = int(east_asian.group("day") or 1)
            if year is not None:
                return datetime(year, month, day)
        except Exception:
            pass

    numeric_parts = [int(p) for p in re.findall(r"\d+", cleaned)]
    if len(numeric_parts) >= 3:
        first, second, third = numeric_parts[0], numeric_parts[1], numeric_parts[2]
        try:
            first = normalize_calendar_year(first)
            third = normalize_calendar_year(third)
            if first and 1900 <= first <= 3000 and 1 <= second <= 12 and 1 <= third <= 31:
                return datetime(first, second, third)
            if 1 <= first <= 31 and 1 <= second <= 12 and third and 1900 <= third <= 3000:
                return datetime(third, second, first)
        except Exception:
            pass

    raw_lower = cleaned.lower()
    simplified = unicodedata.normalize("NFKD", raw_lower)
    simplified = "".join(ch for ch in simplified if not unicodedata.combining(ch))
    month = None
    for alias, alias_month in MONTH_ALIASES.items():
        if alias in raw_lower or alias in simplified:
            month = alias_month
            break
    if month is None:
        return None

    year = None
    for number in numeric_parts:
        ny = normalize_calendar_year(number)
        if ny is not None and 1900 <= ny <= 3000:
            year = ny
            break
    if year is None:
        return None

    day = 1
    for number in numeric_parts:
        if normalize_calendar_year(number) == year:
            continue
        if 1 <= number <= 31:
            day = number
            break
    try:
        return datetime(year, month, day)
    except Exception:
        return None


def format_display_date(value: Any) -> str:
    cleaned = decode_netflix_value(value)
    if not cleaned:
        return "UNKNOWN"
    parsed = parse_localized_date(cleaned)
    if parsed is not None:
        return parsed.strftime("%B %d, %Y").replace(" 0", " ")
    return cleaned


def format_member_since(value: Any) -> str:
    cleaned = decode_netflix_value(value)
    if not cleaned:
        return "UNKNOWN"
    parsed = parse_localized_date(cleaned)
    if parsed is not None:
        return parsed.strftime("%B %Y")
    numeric_parts = re.findall(r"\d+", cleaned)
    if len(numeric_parts) >= 2:
        try:
            month = int(numeric_parts[0])
            year = normalize_calendar_year(numeric_parts[-1])
            if year is not None and 1 <= month <= 12 and 1900 <= year <= 3000:
                return datetime(year, month, 1).strftime("%B %Y")
        except Exception:
            pass
    return cleaned


def normalize_phone_number(value: Any, country_code: Any = None) -> Optional[str]:
    cleaned = decode_netflix_value(value)
    if not cleaned:
        return None
    if str(cleaned).startswith("+"):
        return cleaned
    digits = re.sub(r"\D+", "", str(cleaned))
    if not digits:
        return cleaned
    country = (decode_netflix_value(country_code) or "").strip().upper()
    if country == "IN" and digits.startswith("0") and len(digits) >= 10:
        return f"+91{digits.lstrip('0')}"
    if country == "VN" and digits.startswith("0") and len(digits) >= 9:
        return f"+84{digits.lstrip('0')}"
    return cleaned


def country_code_to_flag(country_code: Any) -> str:
    raw = (decode_netflix_value(country_code) or "").strip()
    if not raw:
        return ""
    upper = raw.upper()
    if len(upper) == 2 and upper.isalpha():
        return "".join(chr(127397 + ord(ch)) for ch in upper)
    name_map = {
        "VNM": "VN", "VIETNAM": "VN", "USA": "US", "UNITED STATES": "US",
        "GBR": "GB", "UNITED KINGDOM": "GB", "JPN": "JP", "KOR": "KR",
        "IDN": "ID", "THA": "TH", "SGP": "SG", "MYS": "MY", "PHL": "PH",
        "IND": "IN", "AUS": "AU", "CAN": "CA", "BRA": "BR",
    }
    mapped = name_map.get(upper)
    if mapped:
        return "".join(chr(127397 + ord(ch)) for ch in mapped)
    return ""


def format_country_with_flag(country_value: Any, unknown_fallback: str = "UNKNOWN") -> str:
    normalized = normalize_output_value(country_value, unknown_fallback=unknown_fallback)
    flag = country_code_to_flag(normalized)
    return f"{normalized} {flag}" if flag else normalized


def sanitize_filename(name: str) -> str:
    cleaned = re.sub(r'[<>:"/\\|?*]+', "_", name).strip(" .")
    return cleaned or "cookie"


def write_text_file(path: str, content: str) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    data = (content if isinstance(content, str) else str(content or "")).encode(
        "utf-8", errors="replace"
    )
    with open(path, "wb") as fh:
        fh.write(data)
        fh.flush()


def extract_first_match(text: str, patterns: Iterable[str], flags: int = 0) -> Optional[str]:
    for pattern in patterns:
        match = re.search(pattern, text, flags)
        if match:
            return decode_netflix_value(match.group(1))
    return None
