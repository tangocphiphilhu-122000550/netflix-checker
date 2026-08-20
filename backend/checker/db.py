"""Supabase / Postgres storage for live Netflix cookies."""

from __future__ import annotations

import contextvars
import hashlib
import os
import re
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from .cookies import extract_cookie_bundles

try:
    import psycopg2
    from psycopg2 import pool
    from psycopg2.extras import RealDictCursor
except ImportError:
    psycopg2 = None
    pool = None
    RealDictCursor = None

# Only healthy subscribed accounts are kept
LIVE_STATUSES = frozenset({"LIVE"})
OPTIONAL_FREE = frozenset()  # never keep FREE

# Dual pools so batch-check workers cannot exhaust connections used by user/admin APIs.
# Without this, ThreadedConnectionPool.getconn() blocks forever under load → whole site hangs.
# Each pool is (ThreadedConnectionPool, Semaphore) — semaphore gives getconn a real timeout.
_api_pool = None  # type: ignore
_worker_pool = None  # type: ignore
_pool_lock = threading.Lock()
_schema_ready = False
_schema_lock = threading.Lock()
_plans_cache: Dict[str, Any] = {"ts": 0.0, "data": []}
_PLANS_TTL = 30.0

# Default role for db_conn(); batch job sets "worker" so API keeps its own pool.
_pool_role: contextvars.ContextVar[str] = contextvars.ContextVar(
    "db_pool_role", default="api"
)


def get_database_url() -> str:
    return (os.environ.get("DATABASE_URL") or "").strip()


def is_db_configured() -> bool:
    url = get_database_url()
    if not url:
        return False
    if "[YOUR-PASSWORD]" in url or "YOUR-PASSWORD" in url:
        return False
    return url.startswith("postgres")


def _normalize_dsn(url: str) -> str:
    url = url.strip()
    if url.startswith("postgres://"):
        url = "postgresql://" + url[len("postgres://") :]
    if "sslmode=" not in url:
        sep = "&" if "?" in url else "?"
        url = f"{url}{sep}sslmode=require"
    if "connect_timeout=" not in url:
        sep = "&" if "?" in url else "?"
        url = f"{url}{sep}connect_timeout=8"
    return url


def _env_int(name: str, default: int, lo: int, hi: int) -> int:
    try:
        return max(lo, min(hi, int(os.environ.get(name) or default)))
    except Exception:
        return default


def _api_pool_max() -> int:
    # Reserved for HTTP handlers — never shared with batch workers.
    # Keep modest: Supabase free pooler has limited concurrent clients.
    return _env_int("DB_POOL_API", 6, 2, 24)


def _worker_pool_max() -> int:
    # Batch check only — must stay low so Supabase + CPU remain free for API.
    return _env_int("DB_POOL_WORKER", 2, 1, 8)


def _make_pool(maxconn: int) -> Tuple[Any, threading.BoundedSemaphore]:
    if psycopg2 is None or pool is None:
        raise RuntimeError("psycopg2-binary not installed")
    if not is_db_configured():
        raise RuntimeError(
            "DATABASE_URL missing or still has [YOUR-PASSWORD]. Edit .env."
        )
    dsn = _normalize_dsn(get_database_url())
    n = max(1, maxconn)
    p = pool.ThreadedConnectionPool(minconn=1, maxconn=n, dsn=dsn)
    # Cap concurrent borrowers so getconn never waits forever under load.
    sem = threading.BoundedSemaphore(n)
    return p, sem


def _get_pool(role: Optional[str] = None) -> Tuple[Any, threading.BoundedSemaphore]:
    """Return (pool, semaphore) for API or worker (lazy-init)."""
    global _api_pool, _worker_pool
    r = (role or _pool_role.get() or "api").lower()
    if r not in {"api", "worker"}:
        r = "api"

    if r == "worker":
        if _worker_pool is not None:
            return _worker_pool
        with _pool_lock:
            if _worker_pool is None:
                _worker_pool = _make_pool(_worker_pool_max())
            return _worker_pool

    if _api_pool is not None:
        return _api_pool
    with _pool_lock:
        if _api_pool is None:
            _api_pool = _make_pool(_api_pool_max())
        return _api_pool


@contextmanager
def use_worker_db():
    """Route all db_conn() calls in this context to the worker pool."""
    token = _pool_role.set("worker")
    try:
        yield
    finally:
        _pool_role.reset(token)


def _borrow_conn(
    p: Any, sem: threading.BoundedSemaphore, timeout_sec: float = 8.0
):
    """
    getconn with a real timeout via semaphore.
    Stock ThreadedConnectionPool.getconn() blocks forever when exhausted —
    that was the main cause of API hang during 200-cookie batches.
    """
    ok = sem.acquire(timeout=max(0.5, timeout_sec))
    if not ok:
        raise TimeoutError(
            f"DB pool exhausted (waited {timeout_sec:.0f}s). "
            "Retry shortly; batch workers use a separate pool."
        )
    try:
        return p.getconn()
    except Exception:
        sem.release()
        raise


def _return_conn(p: Any, sem: threading.BoundedSemaphore, conn: Any) -> None:
    try:
        if getattr(conn, "closed", 0) == 0:
            status = conn.get_transaction_status()
            if status != 0:  # not IDLE
                try:
                    conn.rollback()
                except Exception:
                    pass
        p.putconn(conn)
    except Exception:
        try:
            conn.close()
        except Exception:
            pass
    finally:
        try:
            sem.release()
        except Exception:
            pass


@contextmanager
def db_conn(role: Optional[str] = None, timeout_sec: Optional[float] = None):
    """
    Borrow a pooled connection.
    role: 'api' | 'worker' | None (use context / default api)
    timeout_sec: max wait for a free connection (default: api 5s, worker 15s)
    """
    r = (role or _pool_role.get() or "api").lower()
    if r not in {"api", "worker"}:
        r = "api"
    if timeout_sec is None:
        # Fail fast on API so user never waits 50s behind batch DB pressure.
        timeout_sec = 2.5 if r == "api" else 12.0

    p, sem = _get_pool(r)
    conn = _borrow_conn(p, sem, timeout_sec=timeout_sec)
    try:
        if r == "api":
            try:
                with conn.cursor() as cur:
                    # Cap any single API query; prefer quick error over hung request.
                    cur.execute("SET LOCAL statement_timeout = '4000ms'")
            except Exception:
                pass
        yield conn
    finally:
        _return_conn(p, sem, conn)


def get_connection(role: Optional[str] = None):
    """Borrow from pool. Prefer db_conn(). Caller must put_connection()."""
    r = (role or _pool_role.get() or "api").lower()
    p, sem = _get_pool(r)
    return _borrow_conn(p, sem, timeout_sec=5.0 if r == "api" else 15.0)


def put_connection(conn, role: Optional[str] = None) -> None:
    if conn is None:
        return
    r = (role or _pool_role.get() or "api").lower()
    p, sem = _get_pool(r)
    _return_conn(p, sem, conn)


def init_db() -> None:
    """Create schema once per process."""
    global _schema_ready
    if _schema_ready:
        return
    with _schema_lock:
        if _schema_ready:
            return
        sql = """
        CREATE TABLE IF NOT EXISTS netflix_live_cookies (
            id              BIGSERIAL PRIMARY KEY,
            filename        TEXT,
            email           TEXT,
            plan            TEXT,
            country         TEXT,
            status          TEXT NOT NULL,
            status_label    TEXT,
            cookie_content  TEXT NOT NULL,
            content_hash    TEXT NOT NULL,
            source          TEXT DEFAULT 'admin_bulk',
            checked_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
        );
        CREATE UNIQUE INDEX IF NOT EXISTS uq_netflix_live_cookies_content_hash
            ON netflix_live_cookies (content_hash);
        CREATE INDEX IF NOT EXISTS idx_netflix_live_cookies_email
            ON netflix_live_cookies (email);
        CREATE INDEX IF NOT EXISTS idx_netflix_live_cookies_status
            ON netflix_live_cookies (status);
        CREATE INDEX IF NOT EXISTS idx_netflix_live_cookies_checked_at
            ON netflix_live_cookies (checked_at DESC);
        """
        with db_conn() as conn:
            with conn:
                with conn.cursor() as cur:
                    cur.execute(sql)
        _schema_ready = True


def content_hash(cookie_content: str) -> str:
    normalized = re.sub(r"\s+", "\n", (cookie_content or "").strip())
    return hashlib.sha256(normalized.encode("utf-8", errors="replace")).hexdigest()


def extract_netscape_for_storage(cookie_content: str) -> str:
    bundles = extract_cookie_bundles(cookie_content)
    if bundles:
        return (bundles[0].get("netscape_text") or cookie_content).strip()
    return (cookie_content or "").strip()


def invalidate_plans_cache() -> None:
    _plans_cache["ts"] = 0.0
    _plans_cache["data"] = []
    invalidate_user_live_cache()


# Short in-memory cache for public catalog — survives heavy batch check load.
_user_live_cache: Dict[str, Any] = {"ts": 0.0, "key": "", "data": None}
_USER_LIVE_TTL = float(os.environ.get("USER_LIVE_CACHE_TTL") or 20.0)
_user_live_lock = threading.Lock()


def invalidate_user_live_cache() -> None:
    with _user_live_lock:
        _user_live_cache["ts"] = 0.0
        _user_live_cache["key"] = ""
        _user_live_cache["data"] = None


def upsert_live_cookie(row: Dict[str, Any]) -> Tuple[bool, str]:
    cookie_content = extract_netscape_for_storage(row.get("cookie_content") or "")
    if not cookie_content:
        return False, "skipped"

    chash = content_hash(cookie_content)
    now = datetime.now(timezone.utc)

    sql = """
    INSERT INTO netflix_live_cookies (
        filename, email, plan, country, status, status_label,
        cookie_content, content_hash, source, checked_at, created_at, updated_at
    ) VALUES (
        %(filename)s, %(email)s, %(plan)s, %(country)s, %(status)s, %(status_label)s,
        %(cookie_content)s, %(content_hash)s, %(source)s, %(checked_at)s, %(checked_at)s, %(checked_at)s
    )
    ON CONFLICT (content_hash) DO UPDATE SET
        filename = EXCLUDED.filename,
        email = COALESCE(EXCLUDED.email, netflix_live_cookies.email),
        plan = COALESCE(EXCLUDED.plan, netflix_live_cookies.plan),
        country = COALESCE(EXCLUDED.country, netflix_live_cookies.country),
        status = EXCLUDED.status,
        status_label = EXCLUDED.status_label,
        cookie_content = EXCLUDED.cookie_content,
        checked_at = EXCLUDED.checked_at,
        updated_at = EXCLUDED.checked_at
    RETURNING (xmax = 0) AS inserted;
    """
    payload = {
        "filename": row.get("filename") or "cookie.txt",
        "email": row.get("email"),
        "plan": row.get("plan"),
        "country": row.get("country"),
        "status": row.get("status") or "LIVE",
        "status_label": row.get("status_label"),
        "cookie_content": cookie_content,
        "content_hash": chash,
        "source": row.get("source") or "admin_bulk",
        "checked_at": now,
    }

    with db_conn() as conn:
        with conn:
            with conn.cursor() as cur:
                cur.execute(sql, payload)
                result = cur.fetchone()
                inserted = bool(result and result[0])
                invalidate_plans_cache()
                return True, "inserted" if inserted else "updated"


def _row_is_worth_saving(row: Dict[str, Any]) -> bool:
    status = (row.get("status") or "").upper()
    email = (row.get("email") or "").strip()
    country = (row.get("country") or "").strip()

    def blank(v: str) -> bool:
        return (not v) or v.lower() in {
            "unknown",
            "n/a",
            "na",
            "—",
            "-",
            "null",
            "none",
            "free",
        }

    # HOLD / FREE / DEAD → discard
    if status != "LIVE":
        return False
    if blank(email) and blank(country):
        return False
    return True


def save_live_cookies(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    init_db()
    summary = {
        "saved": 0,
        "inserted": 0,
        "updated": 0,
        "skipped": 0,
        "errors": [],
    }
    for row in rows:
        status = (row.get("status") or "").upper()
        if status not in LIVE_STATUSES and status not in OPTIONAL_FREE:
            summary["skipped"] += 1
            continue
        if not _row_is_worth_saving(row):
            summary["skipped"] += 1
            continue
        if not (row.get("cookie_content") or "").strip():
            summary["skipped"] += 1
            continue
        try:
            ok, action = upsert_live_cookie(row)
            if not ok:
                summary["skipped"] += 1
            else:
                summary["saved"] += 1
                summary[action] = summary.get(action, 0) + 1
        except Exception as exc:
            summary["errors"].append(
                {"filename": row.get("filename"), "error": str(exc)}
            )
    return summary


def _serialize_row(r: Any) -> Dict[str, Any]:
    item = dict(r)
    for k, v in item.items():
        if hasattr(v, "isoformat"):
            item[k] = v.isoformat()
    return item


def list_live_cookies(
    limit: int = 50,
    plan: Optional[str] = None,
    status: Optional[str] = None,
    q: Optional[str] = None,
    offset: int = 0,
    include_plans: bool = True,
) -> Dict[str, Any]:
    """Fast list — no cookie_content, pooled connection, optional plans cache."""
    init_db()
    limit = max(1, min(int(limit), 200))
    offset = max(0, int(offset or 0))

    where = []
    params: List[Any] = []

    plan_f = (plan or "").strip()
    if plan_f and plan_f.lower() not in {"all", "*"}:
        where.append("plan ILIKE %s")
        params.append(f"%{plan_f}%")

    status_f = (status or "").strip().upper()
    if status_f and status_f not in {"ALL", "*"}:
        where.append("status = %s")
        params.append(status_f)

    q_f = (q or "").strip()
    if q_f:
        where.append("(email ILIKE %s OR filename ILIKE %s OR country ILIKE %s)")
        like = f"%{q_f}%"
        params.extend([like, like, like])

    where_sql = (" WHERE " + " AND ".join(where)) if where else ""

    with db_conn() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                f"SELECT COUNT(*) AS c FROM netflix_live_cookies{where_sql}",
                params,
            )
            total = int(cur.fetchone()["c"])

            # Never select cookie_content on list (huge TOAST = multi-second lag)
            cur.execute(
                f"""
                SELECT id, filename, email, plan, country, status, status_label,
                       checked_at, created_at, updated_at
                FROM netflix_live_cookies
                {where_sql}
                ORDER BY id DESC
                LIMIT %s OFFSET %s
                """,
                params + [limit, offset],
            )
            rows = [_serialize_row(r) for r in (cur.fetchall() or [])]

            plans: List[Dict[str, Any]] = []
            if include_plans:
                plans = _list_plan_options_on_cursor(cur)

    return {
        "rows": rows,
        "total": total,
        "limit": limit,
        "offset": offset,
        "plans": plans,
    }


def list_live_cookie_ids(
    plan: Optional[str] = None,
    status: Optional[str] = None,
    q: Optional[str] = None,
    max_ids: int = 5000,
) -> Dict[str, Any]:
    """
    All matching ids (no cookie body) — for admin "select all in DB" / recheck all.
    """
    init_db()
    where = []
    params: List[Any] = []

    plan_f = (plan or "").strip()
    if plan_f and plan_f.lower() not in {"all", "*"}:
        where.append("plan ILIKE %s")
        params.append(f"%{plan_f}%")

    status_f = (status or "").strip().upper()
    if status_f and status_f not in {"ALL", "*"}:
        where.append("status = %s")
        params.append(status_f)

    q_f = (q or "").strip()
    if q_f:
        where.append("(email ILIKE %s OR filename ILIKE %s OR country ILIKE %s)")
        like = f"%{q_f}%"
        params.extend([like, like, like])

    where_sql = (" WHERE " + " AND ".join(where)) if where else ""
    cap = max(1, min(int(max_ids or 5000), 10000))

    with db_conn(role="api", timeout_sec=15.0) as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"SELECT COUNT(*) FROM netflix_live_cookies{where_sql}",
                params,
            )
            total = int(cur.fetchone()[0])
            cur.execute(
                f"""
                SELECT id FROM netflix_live_cookies
                {where_sql}
                ORDER BY id ASC
                LIMIT %s
                """,
                params + [cap],
            )
            ids = [int(r[0]) for r in (cur.fetchall() or [])]

    return {
        "ids": ids,
        "total": total,
        "returned": len(ids),
        "truncated": total > len(ids),
    }


def _list_plan_options_on_cursor(cur) -> List[Dict[str, Any]]:
    now = time.time()
    if _plans_cache["data"] and (now - float(_plans_cache["ts"])) < _PLANS_TTL:
        return list(_plans_cache["data"])
    cur.execute(
        """
        SELECT COALESCE(NULLIF(TRIM(plan), ''), 'Unknown') AS plan,
               COUNT(*)::int AS count
        FROM netflix_live_cookies
        GROUP BY 1
        ORDER BY count DESC, plan ASC
        LIMIT 50
        """
    )
    data = [_serialize_row(r) for r in (cur.fetchall() or [])]
    _plans_cache["data"] = data
    _plans_cache["ts"] = now
    return list(data)


def list_plan_options() -> List[Dict[str, Any]]:
    now = time.time()
    if _plans_cache["data"] and (now - float(_plans_cache["ts"])) < _PLANS_TTL:
        return list(_plans_cache["data"])
    init_db()
    with db_conn() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            return _list_plan_options_on_cursor(cur)


def delete_cookies_by_ids(ids: List[int]) -> Dict[str, Any]:
    clean: List[int] = []
    for raw in ids or []:
        try:
            clean.append(int(raw))
        except Exception:
            continue
    clean = list(dict.fromkeys(clean))
    if not clean:
        return {"deleted": 0, "ids": []}

    init_db()
    with db_conn() as conn:
        with conn:
            with conn.cursor() as cur:
                cur.execute(
                    "DELETE FROM netflix_live_cookies WHERE id IN %s RETURNING id",
                    (tuple(clean),),
                )
                deleted_ids = [int(r[0]) for r in (cur.fetchall() or [])]
    invalidate_plans_cache()
    return {"deleted": len(deleted_ids), "ids": deleted_ids}


def update_live_cookie_meta(
    cookie_id: int,
    *,
    email: Optional[str] = None,
    plan: Optional[str] = None,
    country: Optional[str] = None,
    status: str = "LIVE",
    status_label: Optional[str] = None,
) -> bool:
    """Refresh metadata after a successful re-check (still LIVE)."""
    init_db()
    with db_conn() as conn:
        with conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE netflix_live_cookies
                    SET email = COALESCE(%s, email),
                        plan = COALESCE(%s, plan),
                        country = COALESCE(%s, country),
                        status = %s,
                        status_label = COALESCE(%s, status_label),
                        checked_at = NOW(),
                        updated_at = NOW()
                    WHERE id = %s
                    RETURNING id
                    """,
                    (
                        email,
                        plan,
                        country,
                        status or "LIVE",
                        status_label,
                        int(cookie_id),
                    ),
                )
                ok = bool(cur.fetchone())
    if ok:
        invalidate_plans_cache()
    return ok


def delete_cookies_by_plan(plan: str) -> Dict[str, Any]:
    plan_f = (plan or "").strip()
    if not plan_f or plan_f.lower() in {"all", "*"}:
        return {"deleted": 0, "error": "plan filter required"}

    init_db()
    with db_conn() as conn:
        with conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    DELETE FROM netflix_live_cookies
                    WHERE plan ILIKE %s
                    RETURNING id
                    """,
                    (f"%{plan_f}%",),
                )
                deleted_ids = [int(r[0]) for r in (cur.fetchall() or [])]
    invalidate_plans_cache()
    return {"deleted": len(deleted_ids), "ids": deleted_ids}


def get_cookie_by_id(cookie_id: int) -> Optional[Dict[str, Any]]:
    """Fetch one row including full cookie_content (for login link / view)."""
    init_db()
    with db_conn() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                """
                SELECT id, filename, email, plan, country, status, status_label,
                       cookie_content, checked_at, created_at, updated_at
                FROM netflix_live_cookies
                WHERE id = %s
                """,
                (int(cookie_id),),
            )
            row = cur.fetchone()
            return _serialize_row(row) if row else None


def list_user_live_accounts(
    limit: int = 50,
    plan: Optional[str] = None,
    offset: int = 0,
) -> Dict[str, Any]:
    """Public catalog: LIVE only, no cookie body. Cached ~20s for snappy UX under batch load."""
    limit = max(1, min(int(limit), 200))
    offset = max(0, int(offset or 0))
    plan_f = (plan or "").strip()
    cache_key = f"{limit}:{offset}:{plan_f.lower()}"
    now = time.time()
    with _user_live_lock:
        if (
            _user_live_cache.get("data") is not None
            and _user_live_cache.get("key") == cache_key
            and (now - float(_user_live_cache.get("ts") or 0)) < _USER_LIVE_TTL
        ):
            cached = dict(_user_live_cache["data"])
            cached["cached"] = True
            return cached

    data = _list_user_live(limit, plan_f, offset)
    data["cached"] = False
    with _user_live_lock:
        _user_live_cache["ts"] = time.time()
        _user_live_cache["key"] = cache_key
        _user_live_cache["data"] = {
            "rows": data.get("rows") or [],
            "total": data.get("total") or 0,
            "limit": data.get("limit"),
            "offset": data.get("offset"),
            "plans": data.get("plans") or [],
        }
    return data


def _list_user_live(
    limit: int = 50,
    plan: Optional[str] = None,
    offset: int = 0,
) -> Dict[str, Any]:
    init_db()
    # User can pick page size on FE (20/50/100/200)
    limit = max(1, min(int(limit), 200))
    offset = max(0, int(offset or 0))
    where = ["status = 'LIVE'"]
    params: List[Any] = []
    plan_f = (plan or "").strip()
    if plan_f and plan_f.lower() not in {"all", "*"}:
        where.append("plan ILIKE %s")
        params.append(f"%{plan_f}%")
    where_sql = " WHERE " + " AND ".join(where)
    # timeout_sec short — if worker saturates Supabase, fail quick not hang 50s
    with db_conn(role="api", timeout_sec=2.5) as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                f"SELECT COUNT(*) AS c FROM netflix_live_cookies{where_sql}",
                params,
            )
            total = int(cur.fetchone()["c"])
            cur.execute(
                f"""
                SELECT id, filename, email, plan, country, status, status_label,
                       checked_at, updated_at
                FROM netflix_live_cookies
                {where_sql}
                ORDER BY id DESC
                LIMIT %s OFFSET %s
                """,
                params + [limit, offset],
            )
            rows = [_serialize_row(r) for r in (cur.fetchall() or [])]
            plans = _list_plan_options_on_cursor(cur)
    return {
        "rows": rows,
        "total": total,
        "limit": limit,
        "offset": offset,
        "plans": plans,
    }


def count_live_cookies() -> int:
    if not is_db_configured():
        return 0
    try:
        init_db()
        with db_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT COUNT(*) FROM netflix_live_cookies")
                return int(cur.fetchone()[0])
    except Exception:
        return 0


def db_health() -> Dict[str, Any]:
    if not is_db_configured():
        return {
            "ok": False,
            "configured": False,
            "error": "DATABASE_URL not set or still contains [YOUR-PASSWORD]",
        }
    try:
        init_db()
        with db_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT COUNT(*) FROM netflix_live_cookies")
                total = int(cur.fetchone()[0])
        return {"ok": True, "configured": True, "total_live": total}
    except Exception as exc:
        return {"ok": False, "configured": True, "error": str(exc)}
