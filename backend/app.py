#!/usr/bin/env python3
"""
Netflix Checker — Backend API only (no HTML).

  cd backend
  pip install -r requirements.txt
  python app.py
  → http://127.0.0.1:5050

Frontend is separate (../frontend-next Next.js) — set NEXT_PUBLIC_API_BASE to this host.
"""

from __future__ import annotations

import hmac
import os
import secrets
import time
from functools import wraps

from dotenv import load_dotenv
from flask import Flask, jsonify, request
from flask_cors import CORS

load_dotenv()

from checker.batches import (  # noqa: E402
    MAX_UPLOAD_CHUNK,
    add_items_to_batch,
    create_batch,
    DEFAULT_TIMEOUT,
    DEFAULT_WORKERS,
    MAX_WORKERS,
    get_batch,
    get_batch_item,
    list_batch_items,
    list_batches,
    mark_stale_batches,
    request_stop,
    start_batch_check,
)
from checker.recheck_jobs import (  # noqa: E402
    get_active_job as get_active_recheck_job,
    get_job as get_recheck_job,
    mark_stale_recheck_jobs,
    request_stop as request_recheck_stop,
    start_recheck_job,
)
from checker.cookies import extract_cookie_bundles  # noqa: E402
from checker.db import (  # noqa: E402
    db_health,
    delete_cookies_by_ids,
    delete_cookies_by_plan,
    get_cookie_by_id,
    init_db,
    is_db_configured,
    list_live_cookie_ids,
    list_live_cookies,
    list_user_live_accounts,
)
from checker.nftoken import build_nftoken_links, create_nftoken  # noqa: E402
from checker.service import (  # noqa: E402
    check_cookie_content,
    make_login_links_from_content,
    quick_status_from_result,
    recheck_live_cookie_ids,
)

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 32 * 1024 * 1024
app.secret_key = os.environ.get("ADMIN_SECRET") or secrets.token_hex(32)

# CORS: browser preflight (OPTIONS) must return 2xx + ACAO.
# Note: credentials + origin "*" is invalid in browsers — only enable credentials for explicit origins.
_cors_origins = [
    o.strip()
    for o in (os.environ.get("CORS_ORIGINS") or "*").split(",")
    if o.strip()
]
if not _cors_origins:
    _cors_origins = ["*"]
_cors_creds = not (len(_cors_origins) == 1 and _cors_origins[0] == "*")

CORS(
    app,
    resources={r"/api/*": {"origins": _cors_origins}},
    supports_credentials=_cors_creds,
    expose_headers=["Content-Type"],
    allow_headers=["Content-Type", "X-Admin-Token", "Authorization"],
    methods=["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
    max_age=86400,
)

# Local: 127.0.0.1:5050 · Render: 0.0.0.0 + $PORT
HOST = os.environ.get("CHECKER_HOST", "0.0.0.0")
PORT = int(os.environ.get("PORT") or os.environ.get("CHECKER_PORT", "5050"))

_admin_tokens: dict[str, float] = {}
TOKEN_TTL_SEC = 60 * 60 * 12


def _admin_password() -> str:
    return (os.environ.get("ADMIN_PASSWORD") or "admin123").strip()


def _issue_token() -> str:
    token = secrets.token_urlsafe(32)
    _admin_tokens[token] = time.time() + TOKEN_TTL_SEC
    return token


def _valid_token(token: str | None) -> bool:
    if not token:
        return False
    exp = _admin_tokens.get(token)
    if not exp:
        return False
    if time.time() > exp:
        _admin_tokens.pop(token, None)
        return False
    return True


def _get_admin_token() -> str | None:
    return (
        request.headers.get("X-Admin-Token")
        or request.cookies.get("admin_token")
        or (request.get_json(silent=True) or {}).get("token")
    )


def require_admin(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        # Preflight must not hit 401 — that breaks CORS for Vercel → Render calls.
        if request.method == "OPTIONS":
            return ("", 204)
        if not _valid_token(_get_admin_token()):
            return jsonify({"ok": False, "error": "Unauthorized"}), 401
        return fn(*args, **kwargs)

    return wrapper


@app.after_request
def _cors_after(resp):
    """Ensure API error responses still carry CORS headers for browser clients."""
    origin = request.headers.get("Origin")
    if not origin or not request.path.startswith("/api/"):
        return resp
    allowed = _cors_origins
    if "*" in allowed:
        # With credentials browsers reject *; echo only when not using cookies mode
        if not _cors_creds:
            resp.headers.setdefault("Access-Control-Allow-Origin", "*")
        else:
            resp.headers.setdefault("Access-Control-Allow-Origin", origin)
            resp.headers.setdefault("Access-Control-Allow-Credentials", "true")
            resp.headers.setdefault("Vary", "Origin")
    elif origin in allowed:
        resp.headers.setdefault("Access-Control-Allow-Origin", origin)
        if _cors_creds:
            resp.headers.setdefault("Access-Control-Allow-Credentials", "true")
        resp.headers.setdefault("Vary", "Origin")
    resp.headers.setdefault(
        "Access-Control-Allow-Headers",
        "Content-Type, X-Admin-Token, Authorization",
    )
    resp.headers.setdefault(
        "Access-Control-Allow-Methods",
        "GET, HEAD, POST, PUT, PATCH, DELETE, OPTIONS",
    )
    return resp


@app.get("/")
def root():
    return jsonify(
        {
            "ok": True,
            "service": "netflix-checker-backend",
            "docs": "Use frontend separately. API under /api/*",
            "db": is_db_configured(),
        }
    )


@app.route("/api/health", methods=["GET", "HEAD"])
def health():
    """Liveness probe. HEAD = empty 200 (Render/LB). GET = JSON + DB status."""
    if request.method == "HEAD":
        return ("", 200, {"Cache-Control": "no-store"})
    return jsonify(
        {
            "ok": True,
            "service": "netflix-checker-backend",
            "db": db_health() if is_db_configured() else {"configured": False},
        }
    )


# ---------- public user APIs ----------


@app.get("/api/live-accounts")
def api_live_accounts():
    if not is_db_configured():
        return jsonify({"ok": False, "error": "DB chưa cấu hình", "rows": [], "total": 0}), 400
    try:
        limit = int(request.args.get("limit") or 50)
    except Exception:
        limit = 50
    try:
        offset = int(request.args.get("offset") or 0)
    except Exception:
        offset = 0
    plan = request.args.get("plan") or ""
    try:
        data = list_user_live_accounts(limit=limit, plan=plan, offset=offset)
        return jsonify(
            {
                "ok": True,
                "rows": data.get("rows") or [],
                "total": data.get("total") or 0,
                "plans": data.get("plans") or [],
                "count": len(data.get("rows") or []),
                "cached": bool(data.get("cached")),
            }
        )
    except TimeoutError as exc:
        # Under heavy batch check — prefer fast 503 over 50s hang
        return (
            jsonify(
                {
                    "ok": False,
                    "error": str(exc) or "DB bận — thử lại sau vài giây",
                    "rows": [],
                    "total": 0,
                    "busy": True,
                }
            ),
            503,
            {"Retry-After": "3"},
        )
    except Exception as exc:
        err = str(exc)
        busy = "pool exhausted" in err.lower() or "timeout" in err.lower()
        return (
            jsonify(
                {
                    "ok": False,
                    "error": err,
                    "rows": [],
                    "total": 0,
                    "busy": busy,
                }
            ),
            503 if busy else 500,
        )


@app.get("/api/live-accounts/<int:account_id>/cookie")
def api_live_account_cookie(account_id: int):
    if not is_db_configured():
        return jsonify({"ok": False, "error": "DB chưa cấu hình"}), 400
    try:
        row = get_cookie_by_id(account_id)
        if not row:
            return jsonify({"ok": False, "error": "Không tìm thấy"}), 404
        if str(row.get("status") or "").upper() != "LIVE":
            return jsonify({"ok": False, "error": "Không phải LIVE"}), 400
        return jsonify(
            {
                "ok": True,
                "id": row.get("id"),
                "email": row.get("email"),
                "plan": row.get("plan"),
                "country": row.get("country"),
                "status": row.get("status"),
                "filename": row.get("filename"),
                "cookie_content": row.get("cookie_content") or "",
            }
        )
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500


@app.post("/api/live-accounts/<int:account_id>/login-link")
def api_live_account_login_link(account_id: int):
    """
    Create NFToken login links from a LIVE row.
    Body: { nftoken_mode?, attempts?, consume? }
    consume (default true): delete cookie from DB after success — used once.
    """
    if not is_db_configured():
        return jsonify({"ok": False, "error": "DB chưa cấu hình"}), 400
    data = request.get_json(silent=True) or {}
    mode = str(data.get("nftoken_mode") or "both").lower()
    if mode not in {"pc", "mobile", "both"}:
        mode = "both"
    # Default consume=True: once user takes a login link, remove from catalog
    consume = data.get("consume")
    if consume is None:
        consume = True
    else:
        consume = bool(consume)
    try:
        attempts = max(1, int(data.get("attempts") or 3))
    except Exception:
        attempts = 3
    try:
        row = get_cookie_by_id(account_id)
        if not row:
            return jsonify({"ok": False, "error": "Không tìm thấy"}), 404
        if str(row.get("status") or "").upper() != "LIVE":
            return jsonify({"ok": False, "error": "Không phải LIVE"}), 400
        content = row.get("cookie_content") or ""
        bundles = extract_cookie_bundles(content)
        if not bundles:
            return jsonify({"ok": False, "error": "Cookie DB không hợp lệ"}), 400
        cookies = bundles[0].get("cookies") or {}
        token_data, err = create_nftoken(cookies, attempts=attempts)
        if not token_data or not token_data.get("token"):
            return jsonify({"ok": False, "error": err or "NFToken fail"}), 400
        links = [
            {"label": label, "url": url}
            for label, url in build_nftoken_links(token_data["token"], mode)
        ]
        removed = False
        if consume:
            try:
                del_result = delete_cookies_by_ids([account_id])
                removed = int(del_result.get("deleted") or 0) > 0
            except Exception:
                removed = False
        return jsonify(
            {
                "ok": True,
                "id": row.get("id"),
                "email": row.get("email"),
                "plan": row.get("plan"),
                "token": token_data.get("token"),
                "expires_at_utc": token_data.get("expires_at_utc"),
                "links": links,
                "consumed": removed,
                "message": (
                    "Đã tạo link và xóa cookie khỏi catalog"
                    if removed
                    else "Đã tạo link"
                ),
            }
        )
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500


@app.delete("/api/live-accounts/<int:account_id>")
def api_live_account_delete(account_id: int):
    """Public: remove a LIVE cookie from catalog (user took it / discard)."""
    if not is_db_configured():
        return jsonify({"ok": False, "error": "DB chưa cấu hình"}), 400
    try:
        row = get_cookie_by_id(account_id)
        if not row:
            return jsonify({"ok": False, "error": "Không tìm thấy"}), 404
        result = delete_cookies_by_ids([account_id])
        return jsonify(
            {
                "ok": True,
                "deleted": result.get("deleted") or 0,
                "id": account_id,
                "email": row.get("email"),
                "message": "Đã xóa cookie khỏi DB",
            }
        )
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500


@app.post("/api/check")
def api_check():
    data = request.get_json(silent=True) or {}
    content = (data.get("cookie") or "").strip()
    if not content:
        return jsonify({"ok": False, "error": "Missing cookie"}), 400
    make_token = bool(data.get("make_nftoken", True))
    mode = str(data.get("nftoken_mode") or "both").lower()
    if mode not in {"pc", "mobile", "both"}:
        mode = "both"
    try:
        result = check_cookie_content(
            content,
            make_nftoken=make_token,
            nftoken_mode=mode,
            nftoken_attempts=int(data.get("attempts") or 3),
            timeout=int(data.get("timeout") or 15),
        )
        return jsonify(result)
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500


@app.post("/api/login-link")
def api_login_link():
    data = request.get_json(silent=True) or {}
    content = (data.get("cookie") or "").strip()
    if not content:
        return jsonify({"ok": False, "error": "Missing cookie"}), 400
    mode = str(data.get("nftoken_mode") or "both").lower()
    if mode not in {"pc", "mobile", "both"}:
        mode = "both"
    try:
        result = make_login_links_from_content(
            content, mode=mode, attempts=int(data.get("attempts") or 3)
        )
        return jsonify(result)
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500


@app.post("/api/quick-check")
def api_quick_check():
    data = request.get_json(silent=True) or {}
    content = (data.get("cookie") or "").strip()
    filename = data.get("filename") or "cookie.txt"
    if not content:
        return jsonify(
            {
                "ok": False,
                "filename": filename,
                "status": "DEAD",
                "status_label": "DEAD · empty",
                "message": "Empty",
            }
        ), 400
    try:
        timeout = max(5, int(data.get("timeout") or 10))
    except Exception:
        timeout = 10
    try:
        result = check_cookie_content(
            content, make_nftoken=False, timeout=timeout, fallback=False
        )
        return jsonify(quick_status_from_result(result, filename=filename))
    except Exception as exc:
        return jsonify(
            {
                "ok": False,
                "filename": filename,
                "status": "ERROR",
                "status_label": "ERROR",
                "message": str(exc),
            }
        ), 500


# ---------- admin auth ----------


@app.post("/api/admin/login")
def admin_login():
    data = request.get_json(silent=True) or {}
    password = str(data.get("password") or "")
    expected = _admin_password()
    if not expected or not hmac.compare_digest(password, expected):
        return jsonify({"ok": False, "error": "Sai password admin"}), 401
    return jsonify(
        {
            "ok": True,
            "token": _issue_token(),
            "db_configured": is_db_configured(),
        }
    )


@app.post("/api/admin/logout")
@require_admin
def admin_logout():
    tok = _get_admin_token()
    if tok:
        _admin_tokens.pop(tok, None)
    return jsonify({"ok": True})


@app.get("/api/admin/me")
@require_admin
def admin_me():
    return jsonify({"ok": True, "db_configured": is_db_configured()})


@app.get("/api/admin/db-health")
@require_admin
def admin_db_health():
    return jsonify(db_health())


# ---------- admin: phiếu hàng (batches) ----------
# Flow: POST create (empty) → POST .../items (chunks) → POST .../start (check)


def _parse_upload_items_from_request(max_files: int | None = None) -> list[dict]:
    """Read cookie items from multipart files[] or JSON {items:[...]}."""
    cap = max_files if max_files is not None else MAX_UPLOAD_CHUNK
    files = request.files.getlist("files") or request.files.getlist("files[]")
    if not files and request.files.get("file"):
        files = [request.files.get("file")]
    items: list[dict] = []
    if files:
        for f in files[:cap]:
            if f is None:
                continue
            try:
                content = f.read().decode("utf-8", errors="ignore").strip()
            except Exception:
                continue
            if content:
                items.append(
                    {
                        "filename": f.filename or "cookie.txt",
                        "cookie_content": content,
                    }
                )
        return items

    data = request.get_json(silent=True) or {}
    for it in (data.get("items") or [])[:cap]:
        if not isinstance(it, dict):
            continue
        c = (it.get("cookie_content") or "").strip()
        if c:
            items.append(
                {
                    "filename": it.get("filename") or "cookie.txt",
                    "cookie_content": c,
                }
            )
    return items


@app.post("/api/admin/batches")
@require_admin
def admin_create_batch():
    """
    Tạo phiếu trống (status=ready). Không tự check.
    Body JSON: { name? }  hoặc form field name.
    Optional: kèm files/items lần đầu (≤ MAX_UPLOAD_CHUNK) — vẫn không auto-check.
    """
    if not is_db_configured():
        return jsonify({"ok": False, "error": "DATABASE_URL chưa cấu hình"}), 400

    name = None
    if request.content_type and "multipart" in (request.content_type or ""):
        name = request.form.get("name") or None
    else:
        data = request.get_json(silent=True) or {}
        name = data.get("name")
        if not name:
            name = request.form.get("name")

    items = _parse_upload_items_from_request()
    try:
        batch = create_batch(items=items or None, name=name)
        return jsonify(
            {
                "ok": True,
                "batch": batch,
                "upload_chunk_max": MAX_UPLOAD_CHUNK,
                "message": (
                    f"Đã tạo phiếu · {batch.get('total') or 0} cookie trong DB. "
                    "Tiếp tục Upload chunk hoặc bấm Check khi đủ."
                ),
            }
        )
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500


@app.post("/api/admin/batches/<batch_id>/items")
@require_admin
def admin_add_batch_items(batch_id: str):
    """
    Upload thêm cookie vào phiếu (chunk). multipart files[] hoặc JSON items.
    Không start check.
    """
    if not is_db_configured():
        return jsonify({"ok": False, "error": "DATABASE_URL chưa cấu hình"}), 400

    items = _parse_upload_items_from_request()
    if not items:
        return jsonify({"ok": False, "error": "Thiếu files/items trong chunk"}), 400
    try:
        batch = add_items_to_batch(batch_id, items)
        return jsonify(
            {
                "ok": True,
                "batch": batch,
                "added": batch.get("added"),
                "pending": batch.get("pending"),
                "message": (
                    f"+{batch.get('added')} cookie · tổng {batch.get('total')} · "
                    f"PENDING {batch.get('pending')}"
                ),
            }
        )
    except Exception as exc:
        msg = str(exc)
        code = 404 if "Không tìm thấy" in msg else 400
        return jsonify({"ok": False, "error": msg}), code


@app.post("/api/admin/batches/<batch_id>/start")
@require_admin
def admin_start_batch(batch_id: str):
    """
    Bắt đầu check các item PENDING đã có trong DB của phiếu.
    """
    if not is_db_configured():
        return jsonify({"ok": False, "error": "DATABASE_URL chưa cấu hình"}), 400

    data = request.get_json(silent=True) or {}
    try:
        timeout = max(5, int(data.get("timeout") or DEFAULT_TIMEOUT))
    except Exception:
        timeout = DEFAULT_TIMEOUT
    try:
        workers = max(1, min(MAX_WORKERS, int(data.get("workers") or DEFAULT_WORKERS)))
    except Exception:
        workers = DEFAULT_WORKERS

    try:
        result = start_batch_check(batch_id, timeout=timeout, workers=workers)
        return jsonify(
            {
                "ok": True,
                "batch": result.get("batch"),
                "workers": result.get("workers"),
                "timeout": result.get("timeout"),
                "runner": result.get("runner"),
                "pending": result.get("pending"),
                "message": (
                    f"Đã start check · PENDING {result.get('pending')} · "
                    f"runner={result.get('runner')} · workers={result.get('workers')}"
                ),
            }
        )
    except Exception as exc:
        msg = str(exc)
        code = 404 if "Không tìm thấy" in msg else 400
        return jsonify({"ok": False, "error": msg}), code


@app.get("/api/admin/batches")
@require_admin
def admin_list_batches():
    try:
        batches = list_batches(limit=30)
        active = next(
            (
                b
                for b in batches
                if str(b.get("status") or "").lower() in {"running", "queued"}
            ),
            None,
        )
        return jsonify({"ok": True, "batches": batches, "active_batch": active})
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc), "batches": []}), 500


@app.get("/api/admin/batches/<batch_id>")
@require_admin
def admin_get_batch(batch_id: str):
    batch = get_batch(batch_id)
    if not batch:
        return jsonify({"ok": False, "error": "Không tìm thấy phiếu"}), 404
    return jsonify({"ok": True, "batch": batch})


@app.get("/api/admin/batches/<batch_id>/items")
@require_admin
def admin_batch_items(batch_id: str):
    try:
        limit = int(request.args.get("limit") or 100)
    except Exception:
        limit = 100
    try:
        offset = int(request.args.get("offset") or 0)
    except Exception:
        offset = 0
    status = request.args.get("status") or ""
    try:
        data = list_batch_items(
            batch_id, limit=limit, offset=offset, status=status or None
        )
        batch = get_batch(batch_id)
        return jsonify(
            {
                "ok": True,
                "batch": batch,
                "rows": data.get("rows") or [],
                "total": data.get("total") or 0,
            }
        )
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500


@app.post("/api/admin/batches/<batch_id>/stop")
@require_admin
def admin_stop_batch(batch_id: str):
    request_stop(batch_id)
    return jsonify({"ok": True, "message": "Đã gửi lệnh dừng — xong file hiện tại sẽ stop"})


@app.get("/api/admin/list")
@require_admin
def admin_list_live():
    """Global LIVE catalog (for admin DB view)."""
    if not is_db_configured():
        return jsonify({"ok": False, "error": "DB chưa cấu hình", "rows": []}), 400
    try:
        limit = int(request.args.get("limit") or 50)
    except Exception:
        limit = 50
    try:
        offset = int(request.args.get("offset") or 0)
    except Exception:
        offset = 0
    include_plans = str(request.args.get("plans") or "1") not in {"0", "false"}
    try:
        data = list_live_cookies(
            limit=limit,
            plan=request.args.get("plan") or "",
            status=request.args.get("status") or "",
            q=request.args.get("q") or "",
            offset=offset,
            include_plans=include_plans,
        )
        return jsonify(
            {
                "ok": True,
                "rows": data.get("rows") or [],
                "total": data.get("total") or 0,
                "plans": data.get("plans") or [],
                "count": len(data.get("rows") or []),
            }
        )
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc), "rows": []}), 500


@app.get("/api/admin/list/ids")
@require_admin
def admin_list_live_ids():
    """
    All matching LIVE ids (no pagination) — for select-all-in-DB / recheck-all.
    Query: status, plan, q  (same filters as /list)
    """
    if not is_db_configured():
        return jsonify({"ok": False, "error": "DB chưa cấu hình", "ids": []}), 400
    try:
        data = list_live_cookie_ids(
            plan=request.args.get("plan") or "",
            status=request.args.get("status") or "LIVE",
            q=request.args.get("q") or "",
        )
        return jsonify({"ok": True, **data})
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc), "ids": []}), 500


@app.delete("/api/admin/cookies/<int:cookie_id>")
@require_admin
def admin_delete_one(cookie_id: int):
    try:
        result = delete_cookies_by_ids([cookie_id])
        return jsonify({"ok": True, **result})
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500


@app.post("/api/admin/cookies/delete")
@require_admin
def admin_delete_bulk():
    data = request.get_json(silent=True) or {}
    try:
        if data.get("plan") and not data.get("ids"):
            result = delete_cookies_by_plan(str(data.get("plan")))
            if result.get("error"):
                return jsonify({"ok": False, **result}), 400
            return jsonify({"ok": True, **result})
        result = delete_cookies_by_ids(data.get("ids") or [])
        return jsonify({"ok": True, **result})
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500


@app.post("/api/admin/cookies/recheck")
@require_admin
def admin_recheck_live():
    """
    Sync recheck small batches (≤50). Prefer recheck-job for ALL DB.
    Body: { ids: number[], timeout?: 10, delete_on_error?: false }
    """
    if not is_db_configured():
        return jsonify({"ok": False, "error": "DATABASE_URL chưa cấu hình"}), 400
    data = request.get_json(silent=True) or {}
    raw_ids = data.get("ids") or []
    if not raw_ids:
        return jsonify({"ok": False, "error": "Thiếu ids"}), 400
    try:
        timeout = max(5, min(int(data.get("timeout") or 10), 25))
    except Exception:
        timeout = 10
    delete_on_error = bool(data.get("delete_on_error"))
    try:
        result = recheck_live_cookie_ids(
            raw_ids, timeout=timeout, delete_on_error=delete_on_error
        )
        return jsonify(
            {
                "ok": True,
                **result,
                "message": (
                    f"Recheck xong · giữ {result.get('kept', 0)} · "
                    f"xóa {result.get('deleted', 0)} · "
                    f"bỏ qua {result.get('skipped', 0)} · "
                    f"thiếu {result.get('missing', 0)}"
                ),
            }
        )
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500


@app.post("/api/admin/cookies/recheck-job")
@require_admin
def admin_start_recheck_job():
    """
    Start background recheck on BE (survives closing the browser).
    Body: {
      all?: true,           # all LIVE (optional plan/q filters)
      ids?: number[],       # or explicit ids
      plan?: string,
      q?: string,
      timeout?: 10,
      delete_on_error?: false
    }
    """
    if not is_db_configured():
        return jsonify({"ok": False, "error": "DATABASE_URL chưa cấu hình"}), 400
    data = request.get_json(silent=True) or {}
    all_live = bool(data.get("all"))
    raw_ids = data.get("ids") or []
    plan = str(data.get("plan") or "")
    q = str(data.get("q") or "")
    try:
        timeout = max(5, min(int(data.get("timeout") or 10), 25))
    except Exception:
        timeout = 10
    delete_on_error = bool(data.get("delete_on_error"))
    if not all_live and not raw_ids:
        # default: all live with filters
        all_live = True
    try:
        result = start_recheck_job(
            ids=raw_ids if not all_live else None,
            plan=plan,
            q=q,
            timeout=timeout,
            delete_on_error=delete_on_error,
            all_live=all_live,
        )
        job = result.get("job") or {}
        return jsonify(
            {
                "ok": True,
                "job": job,
                "runner": result.get("runner"),
                "total": result.get("total"),
                "message": (
                    f"Đã start recheck nền · {result.get('total')} TK · "
                    f"tắt web vẫn chạy (runner={result.get('runner')})"
                ),
            }
        )
    except Exception as exc:
        msg = str(exc)
        code = 400
        return jsonify({"ok": False, "error": msg}), code


@app.get("/api/admin/cookies/recheck-job")
@require_admin
def admin_active_recheck_job():
    """Active or none."""
    try:
        job = get_active_recheck_job()
        return jsonify({"ok": True, "job": job})
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc), "job": None}), 500


@app.get("/api/admin/cookies/recheck-job/<job_id>")
@require_admin
def admin_get_recheck_job(job_id: str):
    try:
        job = get_recheck_job(job_id)
        if not job:
            return jsonify({"ok": False, "error": "Không tìm thấy job"}), 404
        return jsonify({"ok": True, "job": job})
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500


@app.post("/api/admin/cookies/recheck-job/<job_id>/stop")
@require_admin
def admin_stop_recheck_job(job_id: str):
    request_recheck_stop(job_id)
    return jsonify(
        {
            "ok": True,
            "message": "Đã gửi stop — xong chunk hiện tại sẽ dừng",
        }
    )


@app.post("/api/admin/init-db")
@require_admin
def admin_init_db():
    try:
        init_db()
        from checker.batches import ensure_batch_tables

        ensure_batch_tables()
        return jsonify({"ok": True})
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500


if __name__ == "__main__":
    print("")
    print("=" * 56)
    print("  Netflix Checker BACKEND (API only)")
    print(f"  http://{HOST}:{PORT}")
    print(f"  DB: {'OK' if is_db_configured() else 'NEED .env DATABASE_URL'}")
    if is_db_configured():
        try:
            n = mark_stale_batches()
            if n:
                print(f"  Cleared {n} stale batch job(s)")
        except Exception as exc:
            print(f"  Stale cleanup: {exc}")
        try:
            n2 = mark_stale_recheck_jobs()
            if n2:
                print(f"  Cleared {n2} stale recheck job(s)")
        except Exception as exc:
            print(f"  Recheck stale cleanup: {exc}")
    print("  Frontend: open ../frontend (separate)")
    print("=" * 56)
    print("")
    app.run(host=HOST, port=PORT, debug=False, use_reloader=False, threaded=True)
