"""
Batch / "phiếu hàng" model (2 bước):
  1) Upload: tạo phiếu + append cookie theo chunk → status ready (chưa check)
  2) Check: admin bấm start → process riêng đọc PENDING từ DB

Performance:
  - Job chạy process riêng (không chia GIL với Flask)
  - Pool DB worker tách khỏi pool API
  - Progress ghi DB debounce; check đọc PENDING theo page (RAM ổn với 2k+)
  - Cap workers + chỉ 1 batch running tại 1 thời điểm
"""

from __future__ import annotations

import multiprocessing
import os
import secrets
import threading
import time
import traceback
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from .db import (
    content_hash,
    db_conn,
    extract_netscape_for_storage,
    init_db,
    is_db_configured,
    upsert_live_cookie,
    use_worker_db,
)
from .service import check_cookie_content, quick_status_from_result

# Low defaults: Render free = ~1 shared CPU. 3+ Netflix TLS workers starve API for seconds.
DEFAULT_WORKERS = max(1, min(4, int(os.environ.get("CHECK_WORKERS") or 2)))
DEFAULT_TIMEOUT = max(5, int(os.environ.get("CHECK_TIMEOUT") or 10))
# Hard ceiling even if admin UI/API asks for more.
MAX_WORKERS = max(1, min(4, int(os.environ.get("CHECK_WORKERS_MAX") or 2)))
# How often to flush batch progress counters to Postgres (seconds / items).
PROGRESS_FLUSH_SEC = float(os.environ.get("CHECK_PROGRESS_FLUSH_SEC") or 2.5)
PROGRESS_FLUSH_EVERY = max(1, int(os.environ.get("CHECK_PROGRESS_FLUSH_EVERY") or 8))
# Pause after each checked cookie so API process can get CPU/DB (ms).
CHECK_YIELD_MS = max(0, min(2000, int(os.environ.get("CHECK_YIELD_MS") or 150)))
# Run batch in a separate OS process so Flask stays responsive (1=on, 0=thread fallback).
USE_PROCESS = str(os.environ.get("CHECK_USE_PROCESS") or "1").strip().lower() not in {
    "0",
    "false",
    "no",
    "off",
}
# Max cookies accepted per upload-items API call (FE should chunk).
MAX_UPLOAD_CHUNK = max(20, min(300, int(os.environ.get("CHECK_UPLOAD_CHUNK") or 150)))
# Pending items loaded per page while checking (keeps RAM flat for 2k+ batches).
CHECK_PAGE_SIZE = max(10, min(100, int(os.environ.get("CHECK_PAGE_SIZE") or 40)))

_stop_flags: Dict[str, bool] = {}
_lock = threading.Lock()
_mem: Dict[str, Dict[str, Any]] = {}
_batch_tables_ready = False
_batch_tables_lock = threading.Lock()
# Serialize batch runners in the API process (queue next after current).
_runner_guard = threading.Lock()
_active_runner: Optional[Any] = None  # Process | Thread


def _now():
    return datetime.now(timezone.utc)


def _clamp_workers(workers: Optional[int]) -> int:
    try:
        w = int(workers if workers is not None else DEFAULT_WORKERS)
    except Exception:
        w = DEFAULT_WORKERS
    return max(1, min(w, MAX_WORKERS))


def request_stop(batch_id: str) -> None:
    """Stop works across process boundary via DB flag."""
    with _lock:
        _stop_flags[batch_id] = True
        if batch_id in _mem:
            _mem[batch_id]["stop_requested"] = True
    try:
        with db_conn(role="api") as conn:
            with conn:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        UPDATE check_batches
                        SET stop_requested = TRUE,
                            message = CASE
                                WHEN COALESCE(message, '') LIKE '%stop requested%' THEN message
                                ELSE COALESCE(message, '') || ' · stop requested'
                            END,
                            updated_at = NOW()
                        WHERE id = %s AND status = 'running'
                        """,
                        (batch_id,),
                    )
    except Exception:
        pass


def is_stop_requested(batch_id: str, force_db: bool = False) -> bool:
    with _lock:
        if _stop_flags.get(batch_id):
            return True
    if not force_db:
        # Cheap path: memory only (same process). Cross-process checks use force_db / periodic.
        return False
    try:
        with db_conn(role="worker", timeout_sec=3.0) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT stop_requested FROM check_batches WHERE id = %s",
                    (batch_id,),
                )
                row = cur.fetchone()
                if row and row[0]:
                    with _lock:
                        _stop_flags[batch_id] = True
                    return True
    except Exception:
        pass
    return False


def ensure_batch_tables() -> None:
    """Create batch tables once per process (not on every list/get)."""
    global _batch_tables_ready
    if _batch_tables_ready:
        return
    with _batch_tables_lock:
        if _batch_tables_ready:
            return
        init_db()
        sql = """
        CREATE TABLE IF NOT EXISTS check_batches (
            id              TEXT PRIMARY KEY,
            name            TEXT,
            status          TEXT NOT NULL DEFAULT 'queued',
            total           INT NOT NULL DEFAULT 0,
            done            INT NOT NULL DEFAULT 0,
            kept            INT NOT NULL DEFAULT 0,
            discarded       INT NOT NULL DEFAULT 0,
            failed          INT NOT NULL DEFAULT 0,
            current_file    TEXT,
            message         TEXT,
            stop_requested  BOOLEAN NOT NULL DEFAULT FALSE,
            created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            finished_at     TIMESTAMPTZ
        );

        CREATE TABLE IF NOT EXISTS check_batch_items (
            id              BIGSERIAL PRIMARY KEY,
            batch_id        TEXT NOT NULL REFERENCES check_batches(id) ON DELETE CASCADE,
            filename        TEXT,
            cookie_content  TEXT NOT NULL,
            content_hash    TEXT,
            status          TEXT NOT NULL DEFAULT 'PENDING',
            status_label    TEXT,
            email           TEXT,
            plan            TEXT,
            country         TEXT,
            message         TEXT,
            checked_at      TIMESTAMPTZ,
            created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
        );

        CREATE INDEX IF NOT EXISTS idx_batch_items_batch_id
            ON check_batch_items (batch_id);
        CREATE INDEX IF NOT EXISTS idx_batch_items_status
            ON check_batch_items (status);
        CREATE INDEX IF NOT EXISTS idx_batches_created
            ON check_batches (created_at DESC);
        """
        with db_conn() as conn:
            with conn:
                with conn.cursor() as cur:
                    cur.execute(sql)
                    # Existing DBs created before stop_requested column
                    cur.execute(
                        """
                        ALTER TABLE check_batches
                        ADD COLUMN IF NOT EXISTS stop_requested BOOLEAN NOT NULL DEFAULT FALSE
                        """
                    )
        _batch_tables_ready = True


def _pct(done: int, total: int) -> int:
    if not total:
        return 0
    return int(round(100 * done / total))


def _prepare_items(items: List[Dict[str, str]]) -> List[Dict[str, str]]:
    prepared: List[Dict[str, str]] = []
    for it in items or []:
        filename = (it.get("filename") or "cookie.txt").strip() or "cookie.txt"
        content = extract_netscape_for_storage(it.get("cookie_content") or "")
        if not content:
            continue
        prepared.append(
            {
                "filename": filename,
                "cookie_content": content,
                "content_hash": content_hash(content),
            }
        )
    return prepared


def create_batch(
    items: Optional[List[Dict[str, str]]] = None,
    name: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Create a phiếu. Status = ready (chưa check).
    Optional initial items (usually empty — FE uploads via add_items_to_batch).
    """
    if not is_db_configured():
        raise RuntimeError("DATABASE_URL not configured")
    ensure_batch_tables()

    batch_id = secrets.token_urlsafe(10)
    now = _now()
    display_name = (name or "").strip() or f"Phiếu {now.strftime('%Y-%m-%d %H:%M:%S')}"
    prepared = _prepare_items(items or [])
    total = len(prepared)
    msg = (
        f"Sẵn sàng · {total} cookie trong DB — bấm Check để chạy"
        if total
        else "Phiếu trống — upload cookie (chunk) rồi bấm Check"
    )

    with db_conn() as conn:
        with conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO check_batches (
                        id, name, status, total, done, kept, discarded, failed,
                        message, stop_requested
                    ) VALUES (%s, %s, 'ready', %s, 0, 0, 0, 0, %s, FALSE)
                    """,
                    (batch_id, display_name, total, msg),
                )
                for p in prepared:
                    cur.execute(
                        """
                        INSERT INTO check_batch_items (
                            batch_id, filename, cookie_content, content_hash,
                            status, status_label, message
                        ) VALUES (%s, %s, %s, %s, 'PENDING', 'PENDING · chờ check', %s)
                        """,
                        (
                            batch_id,
                            p["filename"],
                            p["cookie_content"],
                            p["content_hash"],
                            "Đã lưu DB — chờ Check",
                        ),
                    )

    with _lock:
        _stop_flags[batch_id] = False
        _mem[batch_id] = {
            "id": batch_id,
            "name": display_name,
            "status": "ready",
            "total": total,
            "done": 0,
            "kept": 0,
            "discarded": 0,
            "failed": 0,
            "percent": 0,
            "current_file": None,
            "message": msg,
        }

    return get_batch(batch_id) or {"id": batch_id, "total": total, "status": "ready"}


def add_items_to_batch(
    batch_id: str,
    items: List[Dict[str, str]],
) -> Dict[str, Any]:
    """
    Append cookie items to an existing phiếu (upload phase).
    Rejects if batch is currently running.
    """
    if not is_db_configured():
        raise RuntimeError("DATABASE_URL not configured")
    ensure_batch_tables()

    batch = get_batch(batch_id)
    if not batch:
        raise RuntimeError("Không tìm thấy phiếu")

    st = str(batch.get("status") or "").lower()
    if st == "running":
        raise RuntimeError("Phiếu đang check — không thể upload thêm. Dừng job trước.")

    prepared = _prepare_items(items)
    if not prepared:
        raise RuntimeError("Không có cookie hợp lệ trong chunk")

    if len(prepared) > MAX_UPLOAD_CHUNK:
        raise RuntimeError(
            f"Chunk quá lớn ({len(prepared)}). Tối đa {MAX_UPLOAD_CHUNK} file/request."
        )

    with db_conn() as conn:
        with conn:
            with conn.cursor() as cur:
                for p in prepared:
                    cur.execute(
                        """
                        INSERT INTO check_batch_items (
                            batch_id, filename, cookie_content, content_hash,
                            status, status_label, message
                        ) VALUES (%s, %s, %s, %s, 'PENDING', 'PENDING · chờ check', %s)
                        """,
                        (
                            batch_id,
                            p["filename"],
                            p["cookie_content"],
                            p["content_hash"],
                            "Đã lưu DB — chờ Check",
                        ),
                    )
                cur.execute(
                    "SELECT COUNT(*) FROM check_batch_items WHERE batch_id = %s",
                    (batch_id,),
                )
                total = int(cur.fetchone()[0])
                cur.execute(
                    """
                    SELECT COUNT(*) FROM check_batch_items
                    WHERE batch_id = %s AND status = 'PENDING'
                    """,
                    (batch_id,),
                )
                pending = int(cur.fetchone()[0])
                cur.execute(
                    """
                    UPDATE check_batches
                    SET total = %s,
                        status = 'ready',
                        finished_at = NULL,
                        stop_requested = FALSE,
                        message = %s,
                        updated_at = NOW()
                    WHERE id = %s
                    """,
                    (
                        total,
                        f"Sẵn sàng · {total} cookie · PENDING {pending} — bấm Check",
                        batch_id,
                    ),
                )

    with _lock:
        m = _mem.setdefault(batch_id, {"id": batch_id})
        m["status"] = "ready"
        m["total"] = total
        m["message"] = f"Sẵn sàng · {total} cookie · PENDING {pending}"

    out = get_batch(batch_id) or {}
    out["added"] = len(prepared)
    out["pending"] = pending
    return out


def _count_items_by_status(batch_id: str) -> Dict[str, int]:
    with db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT status, COUNT(*) FROM check_batch_items
                WHERE batch_id = %s
                GROUP BY status
                """,
                (batch_id,),
            )
            counts: Dict[str, int] = {}
            for row in cur.fetchall() or []:
                counts[str(row[0] or "").upper()] = int(row[1])
            return counts


def count_pending(batch_id: str) -> int:
    with db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT COUNT(*) FROM check_batch_items
                WHERE batch_id = %s AND status IN ('PENDING', 'CHECKING')
                """,
                (batch_id,),
            )
            return int(cur.fetchone()[0])


def find_running_batch_id() -> Optional[str]:
    ensure_batch_tables()
    with db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id FROM check_batches
                WHERE status = 'running'
                ORDER BY updated_at DESC
                LIMIT 1
                """
            )
            row = cur.fetchone()
            return str(row[0]) if row else None


def start_batch_check(
    batch_id: str,
    timeout: int = DEFAULT_TIMEOUT,
    workers: int = DEFAULT_WORKERS,
) -> Dict[str, Any]:
    """
    Start check on items already in DB (PENDING only).
    Does not accept file upload.
    """
    if not is_db_configured():
        raise RuntimeError("DATABASE_URL not configured")
    ensure_batch_tables()

    batch = get_batch(batch_id)
    if not batch:
        raise RuntimeError("Không tìm thấy phiếu")

    st = str(batch.get("status") or "").lower()
    if st == "running":
        raise RuntimeError("Phiếu này đang check rồi")

    other = find_running_batch_id()
    if other and other != batch_id:
        raise RuntimeError(
            f"Đang có phiếu khác chạy ({other[:8]}…). Đợi xong hoặc Stop trước."
        )

    pending = count_pending(batch_id)
    if pending <= 0:
        raise RuntimeError("Không còn cookie PENDING để check trong phiếu này")

    workers = _clamp_workers(workers)
    timeout = max(5, int(timeout or DEFAULT_TIMEOUT))

    # Reset stop flag; keep historical kept/discarded for already-checked rows.
    counts = _count_items_by_status(batch_id)
    already_kept = int(counts.get("LIVE", 0))
    already_failed = int(counts.get("ERROR", 0))
    already_discarded = (
        int(counts.get("HOLD", 0))
        + int(counts.get("FREE", 0))
        + int(counts.get("DEAD", 0))
        + already_failed
    )
    already_done = sum(counts.values()) - pending

    with db_conn() as conn:
        with conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE check_batches
                    SET status = 'queued',
                        stop_requested = FALSE,
                        finished_at = NULL,
                        done = %s,
                        kept = %s,
                        discarded = %s,
                        failed = %s,
                        current_file = NULL,
                        message = %s,
                        updated_at = NOW()
                    WHERE id = %s AND status <> 'running'
                    """,
                    (
                        already_done,
                        already_kept,
                        already_discarded,
                        already_failed,
                        f"Chờ check · PENDING {pending}/{batch.get('total') or pending}",
                        batch_id,
                    ),
                )
                if cur.rowcount == 0:
                    raise RuntimeError("Không start được (phiếu đang running?)")

    with _lock:
        _stop_flags[batch_id] = False

    mode = start_batch_job(batch_id, timeout=timeout, workers=workers)
    return {
        "batch": get_batch(batch_id),
        "workers": workers,
        "timeout": timeout,
        "runner": mode,
        "pending": pending,
    }


def update_batch(
    batch_id: str,
    *,
    write_db: bool = True,
    **fields: Any,
) -> None:
    allowed = {
        "status",
        "total",
        "done",
        "kept",
        "discarded",
        "failed",
        "current_file",
        "message",
    }
    sets = []
    vals = []
    for k, v in fields.items():
        if k in allowed:
            sets.append(f"{k} = %s")
            vals.append(v)
    finished = fields.get("finished")
    if not sets and not finished:
        return

    with _lock:
        m = _mem.setdefault(batch_id, {"id": batch_id})
        for k, v in fields.items():
            if k in allowed:
                m[k] = v
        t = int(m.get("total") or 0)
        d = int(m.get("done") or 0)
        m["percent"] = _pct(d, t)

    if not write_db:
        return

    sql_extra = ""
    if finished:
        sql_extra = ", finished_at = NOW()"
    if sets:
        sql = f"UPDATE check_batches SET {', '.join(sets)}, updated_at = NOW(){sql_extra} WHERE id = %s"
        vals.append(batch_id)
    else:
        sql = f"UPDATE check_batches SET updated_at = NOW(){sql_extra} WHERE id = %s"
        vals = [batch_id]

    with db_conn() as conn:
        with conn:
            with conn.cursor() as cur:
                cur.execute(sql, vals)


class _ProgressFlusher:
    """Memory-first counters; flush to DB every N items or every few seconds."""

    def __init__(self, batch_id: str):
        self.batch_id = batch_id
        self._lock = threading.Lock()
        self._since_flush = 0
        self._last_flush = time.time()
        self._pending: Dict[str, Any] = {}

    def touch(self, **fields: Any) -> None:
        with self._lock:
            self._pending.update(fields)
            self._since_flush += 1
            now = time.time()
            should = (
                self._since_flush >= PROGRESS_FLUSH_EVERY
                or (now - self._last_flush) >= PROGRESS_FLUSH_SEC
            )
        # Always refresh in-process memory immediately
        update_batch(self.batch_id, write_db=False, **fields)
        if should:
            self.flush()

    def flush(self, **extra: Any) -> None:
        with self._lock:
            payload = dict(self._pending)
            payload.update(extra)
            self._pending.clear()
            self._since_flush = 0
            self._last_flush = time.time()
        if payload:
            update_batch(self.batch_id, write_db=True, **payload)


def get_batch(batch_id: str) -> Optional[Dict[str, Any]]:
    mem = None
    with _lock:
        if batch_id in _mem:
            mem = dict(_mem[batch_id])

    ensure_batch_tables()
    with db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, name, status, total, done, kept, discarded, failed,
                       current_file, message, created_at, updated_at, finished_at
                FROM check_batches WHERE id = %s
                """,
                (batch_id,),
            )
            row = cur.fetchone()
            if not row:
                return mem
            keys = [
                "id",
                "name",
                "status",
                "total",
                "done",
                "kept",
                "discarded",
                "failed",
                "current_file",
                "message",
                "created_at",
                "updated_at",
                "finished_at",
            ]
            data = dict(zip(keys, row))
            for k in ("created_at", "updated_at", "finished_at"):
                if data.get(k) is not None and hasattr(data[k], "isoformat"):
                    data[k] = data[k].isoformat()

    # Prefer in-memory only when it is *ahead* of DB (same-process thread + debounce).
    # When job runs in a child process, parent _mem stays stale — always trust DB.
    if mem:
        mem_done = int(mem.get("done") or 0)
        db_done = int(data.get("done") or 0)
        mem_st = str(mem.get("status") or "").lower()
        if mem_done > db_done and mem_st in {"running", "queued"}:
            out = dict(data)
            for k in (
                "status",
                "total",
                "done",
                "kept",
                "discarded",
                "failed",
                "current_file",
                "message",
            ):
                if k in mem and mem[k] is not None:
                    out[k] = mem[k]
            out["percent"] = _pct(int(out.get("done") or 0), int(out.get("total") or 0))
            return out

    data["percent"] = _pct(int(data.get("done") or 0), int(data.get("total") or 0))
    return data


def list_batches(limit: int = 30) -> List[Dict[str, Any]]:
    ensure_batch_tables()
    with db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, name, status, total, done, kept, discarded, failed,
                       current_file, message, created_at, updated_at, finished_at
                FROM check_batches
                ORDER BY created_at DESC
                LIMIT %s
                """,
                (max(1, min(limit, 50)),),
            )
            keys = [
                "id",
                "name",
                "status",
                "total",
                "done",
                "kept",
                "discarded",
                "failed",
                "current_file",
                "message",
                "created_at",
                "updated_at",
                "finished_at",
            ]
            out = []
            for row in cur.fetchall() or []:
                data = dict(zip(keys, row))
                for k in ("created_at", "updated_at", "finished_at"):
                    if data.get(k) is not None and hasattr(data[k], "isoformat"):
                        data[k] = data[k].isoformat()
                data["percent"] = _pct(int(data.get("done") or 0), int(data.get("total") or 0))
                out.append(data)
            return out


def list_batch_items(
    batch_id: str,
    limit: int = 100,
    offset: int = 0,
    status: Optional[str] = None,
    include_cookie: bool = False,
) -> Dict[str, Any]:
    """List items in a phiếu. By default no cookie body (fast)."""
    ensure_batch_tables()
    limit = max(1, min(int(limit), 200))
    offset = max(0, int(offset or 0))
    where = ["batch_id = %s"]
    params: List[Any] = [batch_id]
    st = (status or "").strip().upper()
    if st and st not in {"ALL", "*"}:
        where.append("status = %s")
        params.append(st)
    where_sql = " AND ".join(where)
    cols = (
        "id, batch_id, filename, status, status_label, email, plan, country, message, checked_at, updated_at"
        if not include_cookie
        else "id, batch_id, filename, status, status_label, email, plan, country, message, cookie_content, checked_at, updated_at"
    )
    with db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"SELECT COUNT(*) FROM check_batch_items WHERE {where_sql}",
                params,
            )
            total = int(cur.fetchone()[0])
            cur.execute(
                f"""
                SELECT {cols}
                FROM check_batch_items
                WHERE {where_sql}
                ORDER BY id ASC
                LIMIT %s OFFSET %s
                """,
                params + [limit, offset],
            )
            colnames = [d[0] for d in cur.description]
            rows = []
            for r in cur.fetchall() or []:
                item = dict(zip(colnames, r))
                for k, v in list(item.items()):
                    if hasattr(v, "isoformat"):
                        item[k] = v.isoformat()
                rows.append(item)
    return {"rows": rows, "total": total, "limit": limit, "offset": offset}


def get_batch_item(item_id: int) -> Optional[Dict[str, Any]]:
    ensure_batch_tables()
    with db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, batch_id, filename, status, status_label, email, plan, country,
                       message, cookie_content, checked_at, updated_at
                FROM check_batch_items WHERE id = %s
                """,
                (int(item_id),),
            )
            row = cur.fetchone()
            if not row:
                return None
            keys = [
                "id",
                "batch_id",
                "filename",
                "status",
                "status_label",
                "email",
                "plan",
                "country",
                "message",
                "cookie_content",
                "checked_at",
                "updated_at",
            ]
            data = dict(zip(keys, row))
            for k in ("checked_at", "updated_at"):
                if data.get(k) is not None and hasattr(data[k], "isoformat"):
                    data[k] = data[k].isoformat()
            return data


def _set_item(item_id: int, **fields: Any) -> None:
    allowed = {
        "status",
        "status_label",
        "email",
        "plan",
        "country",
        "message",
        "checked_at",
    }
    sets = []
    vals = []
    for k, v in fields.items():
        if k in allowed:
            sets.append(f"{k} = %s")
            vals.append(v)
    if not sets:
        return
    sets.append("updated_at = NOW()")
    vals.append(item_id)
    with db_conn() as conn:
        with conn:
            with conn.cursor() as cur:
                cur.execute(
                    f"UPDATE check_batch_items SET {', '.join(sets)} WHERE id = %s",
                    vals,
                )


def _should_keep(status_row: Dict[str, Any]) -> bool:
    status = (status_row.get("status") or "").upper()
    email = (status_row.get("email") or "").strip()
    country = (status_row.get("country") or "").strip()
    if status != "LIVE":
        return False

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

    return (not blank(email)) or (not blank(country))


def _process_one_item(item: Dict[str, Any], timeout: int) -> Dict[str, Any]:
    """One cookie check. DB writes use worker pool; no intermediate CHECKING row."""
    item_id = item["id"]
    filename = item.get("filename") or f"#{item_id}"
    content = item.get("cookie_content") or ""
    try:
        # Network-bound; do not hold a DB connection while waiting on Netflix.
        result = check_cookie_content(
            content,
            make_nftoken=False,
            timeout=timeout,
            fallback=False,
        )
        status_row = quick_status_from_result(result, filename=filename)
        if _should_keep(status_row):
            _set_item(
                item_id,
                status="LIVE",
                status_label=status_row.get("status_label") or "LIVE",
                email=status_row.get("email"),
                plan=status_row.get("plan"),
                country=status_row.get("country"),
                message=status_row.get("message") or "Kept",
                checked_at=_now(),
            )
            try:
                upsert_live_cookie(
                    {
                        "filename": filename,
                        "email": status_row.get("email"),
                        "plan": status_row.get("plan"),
                        "country": status_row.get("country"),
                        "status": "LIVE",
                        "status_label": status_row.get("status_label"),
                        "cookie_content": content,
                        "source": "batch_job",
                    }
                )
            except Exception:
                pass
            return {"filename": filename, "action": "kept", "status": "LIVE"}
        final_st = (status_row.get("status") or "DEAD").upper()
        if final_st not in {"HOLD", "FREE", "DEAD", "ERROR"}:
            final_st = "DEAD"
        _set_item(
            item_id,
            status=final_st,
            status_label=status_row.get("status_label") or final_st,
            email=status_row.get("email"),
            plan=status_row.get("plan"),
            country=status_row.get("country"),
            message=status_row.get("message") or "Discarded",
            checked_at=_now(),
        )
        return {
            "filename": filename,
            "action": "discarded",
            "status": final_st,
            "message": status_row.get("message"),
        }
    except Exception as exc:
        try:
            _set_item(
                item_id,
                status="ERROR",
                status_label="ERROR",
                message=str(exc),
                checked_at=_now(),
            )
        except Exception:
            pass
        return {"filename": filename, "action": "failed", "message": str(exc)}


def _fetch_pending_page(
    batch_id: str, limit: int = CHECK_PAGE_SIZE
) -> List[Dict[str, Any]]:
    """Next page of PENDING items only — never load full 2k batch into RAM."""
    lim = max(1, min(int(limit), 100))
    with db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, filename, cookie_content, status
                FROM check_batch_items
                WHERE batch_id = %s
                  AND status IN ('PENDING', 'CHECKING')
                ORDER BY id ASC
                LIMIT %s
                """,
                (batch_id, lim),
            )
            rows = []
            for r in cur.fetchall() or []:
                rows.append(
                    {
                        "id": int(r[0]),
                        "filename": r[1],
                        "cookie_content": r[2],
                        "status": r[3],
                    }
                )
            return rows


def _batch_totals(batch_id: str) -> Dict[str, int]:
    """total items + progress counters from DB."""
    with db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT total, done, kept, discarded, failed FROM check_batches WHERE id = %s",
                (batch_id,),
            )
            row = cur.fetchone()
            if not row:
                return {"total": 0, "done": 0, "kept": 0, "discarded": 0, "failed": 0}
            return {
                "total": int(row[0] or 0),
                "done": int(row[1] or 0),
                "kept": int(row[2] or 0),
                "discarded": int(row[3] or 0),
                "failed": int(row[4] or 0),
            }


def process_batch(
    batch_id: str,
    timeout: int = DEFAULT_TIMEOUT,
    workers: int = DEFAULT_WORKERS,
) -> None:
    """
    Heavy path — dedicated process. Reads PENDING pages from DB (safe for 2k+).
    """
    with use_worker_db():
        try:
            workers = _clamp_workers(workers)
            timeout = max(5, int(timeout or DEFAULT_TIMEOUT))
            ensure_batch_tables()

            pending_at_start = count_pending(batch_id)
            base = _batch_totals(batch_id)
            batch_total = int(base["total"] or 0)
            if batch_total <= 0:
                batch_total = pending_at_start

            update_batch(
                batch_id,
                status="running",
                message=(
                    f"BE check x{workers} · timeout {timeout}s · "
                    f"PENDING {pending_at_start}/{batch_total}"
                ),
            )

            if pending_at_start == 0:
                update_batch(
                    batch_id,
                    status="done",
                    message="Không còn PENDING để check",
                    finished=True,
                )
                return

            kept = int(base["kept"] or 0)
            discarded = int(base["discarded"] or 0)
            failed = int(base["failed"] or 0)
            done = int(base["done"] or 0)
            checked_this_run = 0
            progress_lock = threading.Lock()
            flusher = _ProgressFlusher(batch_id)
            last_stop_check = 0.0

            def stop_now() -> bool:
                nonlocal last_stop_check
                now = time.time()
                if now - last_stop_check >= 1.5:
                    last_stop_check = now
                    return is_stop_requested(batch_id, force_db=True)
                return is_stop_requested(batch_id, force_db=False)

            def on_result(res: Dict[str, Any]) -> None:
                nonlocal kept, discarded, failed, done, checked_this_run
                with progress_lock:
                    action = res.get("action")
                    if action == "kept":
                        kept += 1
                    elif action == "discarded":
                        discarded += 1
                    else:
                        failed += 1
                        discarded += 1
                    done += 1
                    checked_this_run += 1
                    flusher.touch(
                        done=done,
                        kept=kept,
                        discarded=discarded,
                        failed=failed,
                        current_file=res.get("filename"),
                        message=(
                            f"[{checked_this_run}/{pending_at_start}] "
                            f"{res.get('filename')} → {res.get('action')}"
                        ),
                    )

            yield_sec = CHECK_YIELD_MS / 1000.0
            with ThreadPoolExecutor(max_workers=workers) as pool:
                while True:
                    if stop_now():
                        flusher.flush(
                            status="stopped",
                            done=done,
                            kept=kept,
                            discarded=discarded,
                            failed=failed,
                            message=(
                                f"Admin dừng · đã check {checked_this_run}/"
                                f"{pending_at_start} PENDING"
                            ),
                            finished=True,
                        )
                        return

                    page = _fetch_pending_page(batch_id, CHECK_PAGE_SIZE)
                    if not page:
                        break

                    # Process page in sub-chunks of worker size
                    sub = max(workers, 1)
                    for i in range(0, len(page), sub):
                        if stop_now():
                            flusher.flush(
                                status="stopped",
                                done=done,
                                kept=kept,
                                discarded=discarded,
                                failed=failed,
                                message=(
                                    f"Admin dừng · đã check {checked_this_run}/"
                                    f"{pending_at_start} PENDING"
                                ),
                                finished=True,
                            )
                            return
                        chunk = page[i : i + sub]
                        futs = [
                            pool.submit(_process_one_item, row, timeout)
                            for row in chunk
                        ]
                        for fut in as_completed(futs):
                            try:
                                on_result(fut.result())
                            except Exception as exc:
                                on_result(
                                    {
                                        "filename": "?",
                                        "action": "failed",
                                        "message": str(exc),
                                    }
                                )
                            # Yield CPU between results so API process can run on 1-core hosts.
                            if yield_sec > 0:
                                time.sleep(yield_sec)
                        # Extra pause between sub-chunks
                        time.sleep(max(0.08, yield_sec))

            flusher.flush(
                status="done",
                done=done,
                kept=kept,
                discarded=discarded,
                failed=failed,
                current_file=None,
                message=(
                    f"Xong. LIVE {kept} · loại {discarded} · lỗi {failed} · "
                    f"check run {checked_this_run}"
                ),
                finished=True,
            )
        except Exception as exc:
            try:
                update_batch(
                    batch_id,
                    status="error",
                    message=f"Job lỗi: {exc}",
                    finished=True,
                )
            except Exception:
                pass
            traceback.print_exc()


def _process_batch_entry(
    batch_id: str, timeout: int, workers: int
) -> None:
    """Entry for multiprocessing spawn (fresh interpreter)."""
    try:
        # Prefer lower CPU priority so Flask/API process stays snappy on 1-CPU hosts.
        try:
            if hasattr(os, "nice"):
                os.nice(10)
        except Exception:
            pass
        try:
            from dotenv import load_dotenv

            load_dotenv()
        except Exception:
            pass
        process_batch(batch_id, timeout=timeout, workers=workers)
    except Exception:
        traceback.print_exc()


def _run_batch_serialized(
    batch_id: str, timeout: int, workers: int
) -> None:
    """
    Ensure only one batch runner at a time inside the API process supervisor.
    Next phiếu waits in queue (status stays queued until this starts).
    """
    global _active_runner
    with _runner_guard:
        try:
            process_batch(batch_id, timeout=timeout, workers=workers)
        finally:
            with _lock:
                if _active_runner is not None and getattr(
                    _active_runner, "name", ""
                ).endswith(batch_id[:8]):
                    _active_runner = None


def start_batch_job(
    batch_id: str,
    timeout: int = DEFAULT_TIMEOUT,
    workers: int = DEFAULT_WORKERS,
) -> str:
    """
    Start batch off the request path.
    Returns mode: 'process' | 'thread'.
    """
    global _active_runner
    workers = _clamp_workers(workers)
    timeout = max(5, int(timeout or DEFAULT_TIMEOUT))

    if USE_PROCESS:
        try:
            ctx = multiprocessing.get_context("spawn")
            p = ctx.Process(
                target=_process_batch_entry,
                args=(batch_id, timeout, workers),
                name=f"batch-{batch_id[:8]}",
                daemon=True,
            )
            p.start()
            with _lock:
                _active_runner = p
            return "process"
        except Exception as exc:
            print(f"[batch] process start failed ({exc}); fallback thread")

    t = threading.Thread(
        target=_run_batch_serialized,
        args=(batch_id, timeout, workers),
        name=f"batch-{batch_id[:8]}",
        daemon=True,
    )
    t.start()
    with _lock:
        _active_runner = t
    return "thread"


# Back-compat alias
def start_batch_thread(
    batch_id: str,
    timeout: int = DEFAULT_TIMEOUT,
    workers: int = DEFAULT_WORKERS,
) -> None:
    start_batch_job(batch_id, timeout=timeout, workers=workers)


def create_batch_and_start(
    items: List[Dict[str, str]],
    name: Optional[str] = None,
    timeout: int = DEFAULT_TIMEOUT,
    workers: int = DEFAULT_WORKERS,
) -> Dict[str, Any]:
    """Legacy: create + start. Prefer create_batch + add_items + start_batch_check."""
    batch = create_batch(items, name=name)
    batch_id = batch["id"]
    if int(batch.get("total") or 0) == 0:
        return {"batch": get_batch(batch_id), "pending": 0}
    return start_batch_check(batch_id, timeout=timeout, workers=workers)


def mark_stale_batches() -> int:
    if not is_db_configured():
        return 0
    try:
        ensure_batch_tables()
        with db_conn() as conn:
            with conn:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        UPDATE check_batches
                        SET status = 'error',
                            message = COALESCE(message,'') || ' · server restarted',
                            finished_at = NOW(),
                            updated_at = NOW()
                        WHERE status IN ('running', 'queued') AND finished_at IS NULL
                        RETURNING id
                        """
                    )
                    return len(cur.fetchall() or [])
    except Exception:
        return 0
