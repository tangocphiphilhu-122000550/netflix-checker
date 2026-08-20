"""Plan classification and account status helpers."""

from __future__ import annotations

from typing import Any, Dict, Optional, Tuple

from .utils import decode_netflix_value, format_boolean_label, int_or_none, normalize_plan_key


CANONICAL_LABELS = {
    "premium": "Premium",
    "standard_with_ads": "Standard With Ads",
    "standard": "Standard",
    "basic": "Basic",
    "mobile": "Mobile",
    "extra_member_premium": "Premium (Extra Member)",
    "free": "Free",
    "duplicate": "Duplicate",
    "unknown": "Unknown",
}


def get_canonical_output_label(plan_key: str) -> str:
    return CANONICAL_LABELS.get(plan_key, "Unknown")


PLAN_ALIASES = {
    "premium": {
        "premium", "premium_plan", "cao_cap", "caocap", "ozel",
        "premium_extra_member", "extra_member_premium",
        "プレミアム", "프리미엄", "พรีเมียม",
    },
    "standard_with_ads": {
        "standard_with_ads", "standardwithads", "estandar_con_anuncios",
        "padrao_com_anuncios", "standard_avec_publicite", "standard_con_pubblicita",
    },
    "standard": {
        "standard", "estandar", "padrao", "standart", "tieuchuan", "tieu_chuan",
        "標準", "스탠다드", "スタンダード",
    },
    "basic": {
        "basic", "basic_with_ads", "basico", "basique", "co_ban", "dasar",
        "基本", "베이직", "ベーシック",
    },
    "mobile": {
        "mobile", "movil", "ponsel", "seluler", "모바일", "モバイル", "มือถือ",
    },
}


def is_extra_member_account(info: Optional[Dict]) -> bool:
    if not isinstance(info, dict):
        return False
    flag = decode_netflix_value(info.get("isExtraMemberAccount"))
    if flag:
        lowered = flag.strip().lower()
        if lowered in {"yes", "true", "1"}:
            return True
        if lowered in {"no", "false", "0"}:
            return False

    markers = (
        "extra member", "miembro extra", "assinante extra", "suscriptor extra",
        "extra_member", "miembro_extra", "membro_extra", "ekstra uye",
        "额外成员", "額外成員", "추가 회원", "thanh vien bo sung",
    )
    for field in ("localizedPlanName", "membershipStatus"):
        value = decode_netflix_value(info.get(field)) or ""
        lowered = value.lower()
        normalized = normalize_plan_key(value)
        if any(m in lowered for m in markers) or any(m in normalized for m in markers):
            return True
        if "extra member" in normalized.replace("_", " "):
            return True
    return False


def is_subscribed_account(info: Optional[Dict]) -> bool:
    status = normalize_plan_key((info or {}).get("membershipStatus"))
    if status == "current_member":
        return True
    return is_extra_member_account(info)


def is_on_hold_account(info: Optional[Dict]) -> bool:
    hold = format_boolean_label((info or {}).get("holdStatus"))
    if hold is not None:
        return hold == "Yes"
    status = normalize_plan_key((info or {}).get("membershipStatus"))
    return any(t in status for t in ("hold", "past_due", "payment_retry", "paused", "suspend"))


def derive_plan_info(info: Dict[str, Any], is_subscribed: bool) -> Tuple[str, str]:
    raw_plan = decode_netflix_value(info.get("localizedPlanName"))
    raw_quality = decode_netflix_value(info.get("videoQuality"))
    streams = int_or_none(info.get("maxStreams"))

    if not is_subscribed and not raw_plan:
        return "free", "Free"

    normalized = normalize_plan_key(raw_plan) if raw_plan else ""
    for canonical, aliases in PLAN_ALIASES.items():
        if normalized in aliases:
            return canonical, get_canonical_output_label(canonical)

    if streams is not None:
        quality_norm = normalize_plan_key(raw_quality) if raw_quality else ""
        if streams >= 4 or quality_norm in {"uhd", "ultra_hd", "4k"}:
            return "premium", "Premium"
        if streams >= 2 or quality_norm in {"hd", "full_hd"}:
            return "standard", "Standard"
        if streams == 1:
            if normalized in {"mobile", "movil", "ponsel"}:
                return "mobile", "Mobile"
            return "basic", "Basic"

    if raw_plan:
        return normalize_plan_key(raw_plan), raw_plan
    if not is_subscribed:
        return "free", "Free"
    return "unknown", "Unknown"


def derive_output_plan_bucket(
    info: Dict[str, Any], is_subscribed: bool
) -> Tuple[str, str, str]:
    plan_key, plan_name = derive_plan_info(info, is_subscribed)
    folder_label = get_canonical_output_label(plan_key)
    display_label = plan_name or folder_label

    if is_subscribed and is_extra_member_account(info):
        extra_key = "extra_member_premium"
        extra_label = get_canonical_output_label(extra_key)
        return extra_key, extra_label, extra_label

    return plan_key, folder_label, display_label
