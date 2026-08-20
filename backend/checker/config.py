"""Configuration loading and helpers."""

from __future__ import annotations

import copy
import os
from typing import Any, Dict, Tuple

try:
    import yaml
except ImportError:
    yaml = None

DEFAULT_CONFIG: Dict[str, Any] = {
    "txt_fields": {
        "name": True,
        "email": True,
        "max_streams": True,
        "plan_price": True,
        "plan": True,
        "country": True,
        "member_since": True,
        "next_billing": True,
        "extra_members": True,
        "payment_method": True,
        "card": True,
        "phone": True,
        "quality": True,
        "hold_status": True,
        "email_verified": True,
        "membership_status": True,
        "profiles": True,
        "user_guid": True,
    },
    "nftoken": False,
    "add_emojis": "webhook",
    "notifications": {
        "webhook": {
            "enabled": False,
            "url": "",
            "mode": "full",
            "plans": "all",
        },
        "telegram": {
            "enabled": False,
            "bot_token": "",
            "chat_id": "",
            "mode": "full",
            "plans": "all",
        },
    },
    "display": {"mode": "log"},
    "retries": {
        "error_proxy_attempts": 3,
        "nftoken_attempts": 1,
    },
    "performance": {
        "request_timeout_seconds": 15,
        "fallback_account_page": True,
        "retry_incomplete_info": False,
        "nftoken_for_free": False,
    },
}

CONFIG_PATH = "config.yml"


def merge_config(default_cfg: Dict[str, Any], user_cfg: Any) -> Dict[str, Any]:
    merged = copy.deepcopy(default_cfg)
    if not isinstance(user_cfg, dict):
        return merged
    for key, value in user_cfg.items():
        if key in merged and isinstance(merged[key], dict) and isinstance(value, dict):
            merged[key] = merge_config(merged[key], value)
        else:
            merged[key] = value
    return merged


def load_config(path: str = CONFIG_PATH) -> Tuple[Dict[str, Any], str]:
    if not os.path.exists(path):
        return copy.deepcopy(DEFAULT_CONFIG), "default"
    if yaml is None:
        print("Warning: PyYAML not installed. Run: pip install -r requirements.txt")
        return copy.deepcopy(DEFAULT_CONFIG), "default"
    try:
        with open(path, "r", encoding="utf-8") as fh:
            user_config = yaml.safe_load(fh) or {}
        return merge_config(DEFAULT_CONFIG, user_config), path
    except Exception as exc:
        print(f"Warning: invalid {path} ({exc}). Using defaults.")
        return copy.deepcopy(DEFAULT_CONFIG), "default"


def get_nftoken_mode(config: Dict[str, Any]) -> str:
    raw = config.get("nftoken", False)
    if isinstance(raw, bool):
        return "both" if raw else "false"
    mode = str(raw).strip().lower()
    if mode in {"false", "off", "none", "disabled", "0"}:
        return "false"
    if mode in {"pc", "desktop", "computer"}:
        return "pc"
    if mode in {"mobile", "phone"}:
        return "mobile"
    if mode in {"both", "all", "true", "on", "1"}:
        return "both"
    return "false"


def get_add_emojis_mode(config: Dict[str, Any]) -> str:
    raw = (config or {}).get("add_emojis", "webhook")
    if isinstance(raw, bool):
        return "both" if raw else "false"
    mode = str(raw).strip().lower()
    if mode in {"false", "off", "none", "0", "no"}:
        return "false"
    if mode in {"txt", "file", "output"}:
        return "txt"
    if mode in {"webhook", "notify", "notification", "telegram", "tg"}:
        return "webhook"
    if mode in {"both", "all", "true", "on", "1"}:
        return "both"
    return "webhook"


def should_add_emojis(config: Dict[str, Any], target: str) -> bool:
    mode = get_add_emojis_mode(config)
    target = str(target or "").strip().lower()
    if target == "txt":
        return mode in {"txt", "both"}
    if target in {"webhook", "notifications", "notify", "telegram"}:
        return mode in {"webhook", "both"}
    return False


def print_config_summary(config: Dict[str, Any], source: str) -> None:
    fields = config.get("txt_fields", {})
    enabled = [k for k, v in fields.items() if v]
    webhook = config.get("notifications", {}).get("webhook", {})
    telegram = config.get("notifications", {}).get("telegram", {})
    perf = config.get("performance", {})
    retries = config.get("retries", {})
    print("Active Config")
    print(f"- Config: {source}")
    print(f"- TXT fields: {', '.join(enabled) if enabled else 'none'}")
    print(f"- NFToken: {get_nftoken_mode(config)}")
    print(f"- Emojis: {get_add_emojis_mode(config)}")
    print(f"- Webhook: {'ON' if webhook.get('enabled') else 'OFF'}")
    print(f"- Telegram: {'ON' if telegram.get('enabled') else 'OFF'}")
    print(f"- Display: {config.get('display', {}).get('mode', 'simple')}")
    print(f"- Retries: {retries.get('error_proxy_attempts', 3)}")
    print(f"- Timeout: {perf.get('request_timeout_seconds', 15)}s")
    print("")
