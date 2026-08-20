"""Shared service used by CLI and web UI."""

from __future__ import annotations

from typing import Any, Dict, List, Optional

import requests

from .cookies import apply_cookies_to_session, extract_cookie_bundles
from .extract import extract_info, has_complete_account_info, merge_info
from .nftoken import build_nftoken_links, create_nftoken, has_usable_nftoken
from .notify import build_account_detail_lines
from .plans import (
    derive_output_plan_bucket,
    is_on_hold_account,
    is_subscribed_account,
)
from .utils import decode_netflix_value

# Imported lazily in recheck to avoid circular imports with db


USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)


def _is_blank_value(value: Any) -> bool:
    if value is None:
        return True
    text = str(value).strip()
    if not text:
        return True
    return text.lower() in {
        "unknown",
        "n/a",
        "na",
        "null",
        "none",
        "—",
        "-",
        "anonymous",
        "free",  # plan label alone is not identity
    }


def is_usable_account_info(info: Optional[Dict[str, Any]], is_subscribed: bool = False) -> tuple[bool, str]:
    """Reject half-dead cookies (FREE + all UNKNOWN / ANONYMOUS).

    A usable cookie must look like a real logged-in account:
    - membership must not be ANONYMOUS
    - need at least email, or (country + membership/plan signal)
    - FREE accounts still need real identity fields (email or country)
    """
    if not info:
        return False, "empty account info"

    membership = (decode_netflix_value(info.get("membershipStatus")) or "").strip()
    membership_l = membership.lower()
    if membership_l in {"anonymous", "anon", "guest"}:
        return False, "membership ANONYMOUS — cookie die / không login thật"

    email = decode_netflix_value(info.get("email"))
    country = decode_netflix_value(info.get("countryOfSignup"))
    plan = decode_netflix_value(info.get("localizedPlanName"))
    name = decode_netflix_value(info.get("accountOwnerName"))
    user_guid = decode_netflix_value(info.get("userGuid"))

    has_email = not _is_blank_value(email) and "@" in str(email)
    has_country = not _is_blank_value(country) and str(country).upper() not in {"UNKNOWN", "NULL"}
    has_plan = not _is_blank_value(plan) and str(plan).lower() not in {"free", "unknown"}
    has_name = not _is_blank_value(name)
    has_guid = not _is_blank_value(user_guid)
    has_membership = bool(membership) and membership_l not in {"anonymous", "null", "unknown"}

    # Strong identity: email is best
    if has_email:
        return True, "ok"

    # Subscribed without email still ok if country + plan/membership present
    if is_subscribed and has_country and (has_plan or has_membership or has_guid or has_name):
        return True, "ok"

    # FREE / weak: require country + another real field (not just empty free shell)
    if has_country and (has_membership or has_guid or has_name or has_plan):
        return True, "ok"

    # All UNKNOWN / Free shell → dead cookie
    return (
        False,
        "cookie hỏng — FREE/UNKNOWN (không có email/country/plan thật)",
    )


def is_savable_to_db(status: str, row: Optional[Dict[str, Any]] = None) -> bool:
    """Only real LIVE (subscribed, not on-hold) is stored. HOLD/FREE discarded."""
    status_u = (status or "").upper()
    if status_u == "HOLD":
        return False
    if status_u == "LIVE":
        if row:
            email = row.get("email")
            country = row.get("country")
            if _is_blank_value(email) and _is_blank_value(country):
                return False
        return True
    return False


def fetch_account_info(
    cookies: Dict[str, str],
    timeout: int = 20,
    fallback: bool = True,
) -> Dict[str, Any]:
    session = requests.Session()
    apply_cookies_to_session(session, cookies)
    headers = {
        "User-Agent": USER_AGENT,
        "Accept-Language": "en-US,en;q=0.9",
        # gzip = smaller HTML, faster than identity
        "Accept-Encoding": "gzip, deflate",
        "Accept": "text/html,application/xhtml+xml",
    }
    # tuple timeout: (connect, read) — fail faster on bad network
    req_timeout = (min(5, timeout), timeout) if isinstance(timeout, (int, float)) else timeout

    try:
        response = session.get(
            "https://www.netflix.com/account/membership",
            headers=headers,
            timeout=req_timeout,
        )
    except requests.exceptions.Timeout:
        return {"ok": False, "error": "timeout", "status_code": None}
    except requests.exceptions.RequestException as exc:
        return {"ok": False, "error": f"network: {exc.__class__.__name__}", "status_code": None}

    status = response.status_code
    if status != 200 or not response.text:
        return {
            "ok": False,
            "error": f"HTTP {status}" if status else "empty response",
            "status_code": status,
        }

    info = extract_info(response.text)
    if fallback and not has_complete_account_info(info):
        try:
            fallback_resp = session.get(
                "https://www.netflix.com/YourAccount",
                headers=headers,
                timeout=req_timeout,
            )
            if fallback_resp.status_code == 200 and fallback_resp.text:
                info = merge_info(info, extract_info(fallback_resp.text))
        except Exception:
            pass

    is_sub = is_subscribed_account(info)
    usable, reason = is_usable_account_info(info, is_subscribed=is_sub)
    if not usable:
        return {
            "ok": False,
            "error": reason,
            "status_code": status,
            "info": info,
        }

    return {"ok": True, "status_code": status, "info": info}


def info_to_fields(info: Dict[str, Any], is_subscribed: bool) -> List[Dict[str, str]]:
    config = {
        "txt_fields": {
            "name": True,
            "email": True,
            "plan": True,
            "country": True,
            "member_since": True,
            "quality": True,
            "max_streams": True,
            "plan_price": True,
            "next_billing": True,
            "payment_method": True,
            "card": True,
            "phone": True,
            "hold_status": True,
            "extra_members": True,
            "email_verified": True,
            "membership_status": True,
            "profiles": True,
            "user_guid": True,
        }
    }
    lines = build_account_detail_lines(
        config,
        info,
        is_subscribed,
        use_emojis=False,
        include_country_flag=True,
        show_all=True,
    )
    fields = []
    for line in lines:
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        fields.append({"label": key.strip(), "value": value.strip()})
    return fields


def quick_status_from_result(check_result: Dict[str, Any], filename: str = "") -> Dict[str, Any]:
    """Compact status row for bulk table (LIVE / DEAD / ERROR)."""
    base = {
        "filename": filename,
        "status": "ERROR",
        "status_label": "ERROR",
        "plan": None,
        "email": None,
        "country": None,
        "message": check_result.get("error") or "",
        "ok": False,
        "savable": False,
    }
    results = check_result.get("results") or []
    if not results:
        base["message"] = check_result.get("error") or "No cookie sets found"
        base["status"] = "DEAD"
        base["status_label"] = "DEAD / invalid"
        return base

    # Prefer first successful bundle; else first failure reason
    ok_item = next((r for r in results if r.get("ok")), None)
    if ok_item:
        subscribed = bool(ok_item.get("subscribed"))
        on_hold = bool(ok_item.get("on_hold"))
        email = ok_item.get("email")
        country = ok_item.get("country")
        plan = ok_item.get("plan_label")

        # Extra guard: FREE shell without identity → DEAD
        if not subscribed and _is_blank_value(email) and _is_blank_value(country):
            return {
                "filename": filename,
                "status": "DEAD",
                "status_label": "DEAD · free/unknown",
                "plan": plan,
                "email": email,
                "country": country,
                "message": "Cookie hỏng — Free + thiếu email/country (bỏ, không lưu)",
                "ok": False,
                "savable": False,
                "bundles": len(results),
                "live_bundles": 0,
            }

        if on_hold:
            status, label = "HOLD", "LIVE · On Hold"
        elif subscribed:
            status, label = "LIVE", "LIVE · Subscribed"
        else:
            status, label = "FREE", "LIVE · Free / no sub"

        row = {
            "filename": filename,
            "status": status,
            "status_label": label,
            "plan": plan,
            "email": email,
            "country": country,
            "message": ok_item.get("status") or label,
            "ok": True,
            "bundles": len(results),
            "live_bundles": sum(1 for r in results if r.get("ok")),
        }
        row["savable"] = is_savable_to_db(status, row)
        if status == "FREE" and not row["savable"]:
            row["status"] = "DEAD"
            row["status_label"] = "DEAD · free junk"
            row["message"] = "Free nhưng không có email — bỏ"
            row["ok"] = False
        return row

    fail = results[0]
    err = fail.get("error") or check_result.get("error") or "failed"
    err_l = str(err).lower()
    if "anonymous" in err_l or "hỏng" in err_l or "unknown" in err_l:
        status, label = "DEAD", "DEAD · invalid"
    elif "missing" in err_l or "invalid" in err_l or "incomplete" in err_l:
        status, label = "DEAD", "DEAD"
    elif "403" in err_l or "429" in err_l or "timeout" in err_l or "network" in err_l or "proxy" in err_l:
        status, label = "ERROR", "ERROR · network"
    else:
        status, label = "DEAD", "DEAD"
    return {
        "filename": filename,
        "status": status,
        "status_label": label,
        "plan": None,
        "email": None,
        "country": None,
        "message": err,
        "ok": False,
        "savable": False,
        "bundles": len(results),
        "live_bundles": 0,
    }


def check_cookie_content(
    content: str,
    make_nftoken: bool = True,
    nftoken_mode: str = "both",
    nftoken_attempts: int = 3,
    timeout: int = 20,
    fallback: bool = True,
) -> Dict[str, Any]:
    bundles = extract_cookie_bundles(content)
    if not bundles:
        return {
            "ok": False,
            "error": "No valid Netflix cookies found. Need NetflixId (Netscape/JSON/raw).",
            "results": [],
        }

    results: List[Dict[str, Any]] = []
    for bundle in bundles:
        cookies = bundle["cookies"]
        item: Dict[str, Any] = {
            "index": bundle.get("index", 1),
            "total": bundle.get("total", 1),
            "has_netflix_id": bool(cookies.get("NetflixId")),
            "cookie_names": sorted(cookies.keys()),
        }

        fetched = fetch_account_info(cookies, timeout=timeout, fallback=fallback)
        if not fetched.get("ok"):
            item["ok"] = False
            item["error"] = fetched.get("error", "unknown error")
            item["status_code"] = fetched.get("status_code")
            results.append(item)
            continue

        info = fetched["info"]
        is_sub = is_subscribed_account(info)
        on_hold = is_sub and is_on_hold_account(info)
        plan_key, folder_label, display_label = derive_output_plan_bucket(info, is_sub)

        item.update(
            {
                "ok": True,
                "subscribed": is_sub,
                "on_hold": on_hold,
                "plan_key": plan_key,
                "plan_label": display_label or folder_label,
                "status": (
                    "On Hold"
                    if on_hold
                    else ("Subscribed" if is_sub else "Free / No subscription")
                ),
                "email": decode_netflix_value(info.get("email")),
                "country": decode_netflix_value(info.get("countryOfSignup")),
                "fields": info_to_fields(info, is_sub),
            }
        )

        # Always expose raw cookie for browser import (most reliable PC login)
        item["netscape_cookie"] = bundle.get("netscape_text") or ""
        item["login_tips"] = [
            "Cách ổn định nhất trên PC: import cookie bằng Cookie-Editor (không dùng NFToken).",
            "Nếu dùng NFToken: mở Incognito, xóa cookie Netflix cũ, thử link PC Login trước.",
            "Đừng mở link khi browser đang login account Netflix khác.",
            "Token hết hạn nhanh — tạo lại nếu vừa fail.",
        ]

        if make_nftoken:
            token_data, token_err = create_nftoken(cookies, attempts=nftoken_attempts)
            if has_usable_nftoken(token_data):
                links = [
                    {"label": label, "url": url}
                    for label, url in build_nftoken_links(
                        token_data.get("token"), nftoken_mode
                    )
                ]
                item["nftoken"] = {
                    "ok": True,
                    "token": token_data["token"],
                    "expires_at_utc": token_data.get("expires_at_utc"),
                    "links": links,
                }
            else:
                item["nftoken"] = {"ok": False, "error": token_err or "failed"}
        else:
            item["nftoken"] = None

        results.append(item)

    any_ok = any(r.get("ok") for r in results)
    return {
        "ok": any_ok,
        "error": None if any_ok else "All cookie sets failed",
        "count": len(results),
        "results": results,
    }


def recheck_live_cookie_ids(
    ids: List[int],
    timeout: int = 10,
    delete_on_error: bool = False,
    max_ids: int = 50,
) -> Dict[str, Any]:
    """
    Re-verify LIVE rows in DB (daily admin hygiene).
    - Still LIVE → update meta, keep
    - DEAD / FREE / HOLD / invalid → delete from DB
    - Network ERROR → skip by default (avoid false delete); set delete_on_error=True to purge
    """
    from .db import delete_cookies_by_ids, get_cookie_by_id, update_live_cookie_meta

    clean: List[int] = []
    for raw in ids or []:
        try:
            clean.append(int(raw))
        except Exception:
            continue
    cap = max(1, min(int(max_ids or 50), 200))
    clean = list(dict.fromkeys(clean))[:cap]

    results: List[Dict[str, Any]] = []
    summary: Dict[str, Any] = {
        "ok": True,
        "total": len(clean),
        "kept": 0,
        "deleted": 0,
        "skipped": 0,
        "missing": 0,
        "results": results,
    }

    timeout = max(5, min(int(timeout or 10), 25))

    for cid in clean:
        row = get_cookie_by_id(cid)
        if not row:
            summary["missing"] += 1
            results.append(
                {
                    "id": cid,
                    "action": "missing",
                    "status": "MISSING",
                    "message": "Không tìm thấy trong DB",
                }
            )
            continue

        content = (row.get("cookie_content") or "").strip()
        email0 = row.get("email")
        filename = row.get("filename") or f"#{cid}"
        if not content:
            delete_cookies_by_ids([cid])
            summary["deleted"] += 1
            results.append(
                {
                    "id": cid,
                    "email": email0,
                    "filename": filename,
                    "action": "deleted",
                    "status": "DEAD",
                    "message": "Cookie rỗng — đã xóa",
                }
            )
            continue

        try:
            check = check_cookie_content(
                content,
                make_nftoken=False,
                timeout=timeout,
                fallback=False,
            )
            status_row = quick_status_from_result(check, filename=filename)
        except Exception as exc:
            status_row = {
                "status": "ERROR",
                "status_label": "ERROR",
                "message": str(exc),
                "email": email0,
                "plan": row.get("plan"),
                "country": row.get("country"),
                "savable": False,
            }

        st = (status_row.get("status") or "ERROR").upper()
        msg = status_row.get("message") or status_row.get("status_label") or st
        savable = is_savable_to_db(st, status_row)

        if st == "LIVE" and savable:
            try:
                update_live_cookie_meta(
                    cid,
                    email=status_row.get("email") or email0,
                    plan=status_row.get("plan") or row.get("plan"),
                    country=status_row.get("country") or row.get("country"),
                    status="LIVE",
                    status_label=status_row.get("status_label") or "LIVE · recheck",
                )
            except Exception:
                pass
            summary["kept"] += 1
            results.append(
                {
                    "id": cid,
                    "email": status_row.get("email") or email0,
                    "filename": filename,
                    "action": "kept",
                    "status": "LIVE",
                    "plan": status_row.get("plan"),
                    "country": status_row.get("country"),
                    "message": msg,
                }
            )
            continue

        # Temporary network/proxy issues — do not delete unless forced
        is_soft_error = st == "ERROR" or any(
            k in str(msg).lower()
            for k in ("timeout", "network", "proxy", "429", "502", "503", "504")
        )
        if is_soft_error and not delete_on_error:
            summary["skipped"] += 1
            results.append(
                {
                    "id": cid,
                    "email": email0,
                    "filename": filename,
                    "action": "skipped",
                    "status": st,
                    "message": f"Lỗi tạm — giữ DB · {msg}",
                }
            )
            continue

        # DEAD / FREE / HOLD / hard fail → remove from catalog
        try:
            delete_cookies_by_ids([cid])
        except Exception as exc:
            summary["skipped"] += 1
            results.append(
                {
                    "id": cid,
                    "email": email0,
                    "filename": filename,
                    "action": "error",
                    "status": st,
                    "message": f"Xóa fail: {exc}",
                }
            )
            continue

        summary["deleted"] += 1
        results.append(
            {
                "id": cid,
                "email": email0,
                "filename": filename,
                "action": "deleted",
                "status": st,
                "message": msg,
            }
        )

    return summary


def make_login_links_from_content(
    content: str,
    mode: str = "both",
    attempts: int = 3,
) -> Dict[str, Any]:
    bundles = extract_cookie_bundles(content)
    if not bundles:
        return {
            "ok": False,
            "error": "No valid Netflix cookies found. Need NetflixId.",
            "results": [],
        }

    results = []
    for bundle in bundles:
        cookies = bundle["cookies"]
        data, err = create_nftoken(cookies, attempts=attempts)
        if has_usable_nftoken(data):
            results.append(
                {
                    "ok": True,
                    "index": bundle.get("index", 1),
                    "token": data["token"],
                    "expires_at_utc": data.get("expires_at_utc"),
                    "netscape_cookie": bundle.get("netscape_text") or "",
                    "links": [
                        {"label": label, "url": url}
                        for label, url in build_nftoken_links(data["token"], mode)
                    ],
                    "how_to": [
                        "Link NFToken = login không cần email/password (magic link).",
                        "Mở Incognito (Ctrl+Shift+N) — không cookie Netflix cũ.",
                        "Dán link 'PC Login (browser-safe)' vào thanh địa chỉ → Enter.",
                        "Thấy chọn profile = thành công. Hết hạn thì tạo lại.",
                    ],
                }
            )
        else:
            results.append(
                {
                    "ok": False,
                    "index": bundle.get("index", 1),
                    "error": err or "failed",
                }
            )

    any_ok = any(r.get("ok") for r in results)
    first_err = None
    for r in results:
        if not r.get("ok") and r.get("error"):
            first_err = r["error"]
            break
    return {
        "ok": any_ok,
        "error": None if any_ok else (first_err or "Could not create login links"),
        "results": results,
    }
