"""
Background recheck jobs for LIVE catalog hygiene.

Unlike FE-driven chunk loops (stop when tab closes), these run on the BE
in a child process — closing the admin page does not stop the job.
"""

from __future__ import annotations

import multiprocessing
import os
import secrets
import threading
import time
import traceback
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from .db import db_conn, is_db_configured, list_live_cookie_ids, use_worker_db
from .service import recheck_live_cookie_ids

DEFAULT_TIMEOUT = max(5, min(25, int(os.environ.get("CHECK_TIMEOUT") or 10)))
USE_PROCESS = str(os.environ.get("CHECK_USE_PROCESS") or "1").strip().lower() not in {
    "0",
    "false",
    "no",
    "off",
}
CHUNK = max(5, min(30, int(os.environ.get("RECHECK_JOB_CHUNK") or 12)))
YIELD_SEC = max(0.0, float(os.environ.get("RECHECK_JOB_YIELD") or 0.12))

_lock = threading.Lock()
_mem: Dict[str, Dict[str, Any]] = {}
_stop: Dict[str, bool] = {}
_tables_ready = False
_tables_lock = threading.Lock()


def _now():
    return datetime.now(timezone.utc)


def ensure_recheck_tables() -> None:
    global _tables_ready
    if _tables_ready:
        return
    with _tables_lock:
        if _tables_ready:
            return
        sql = """
        CREATE TABLE IF NOT EXISTS recheck_jobs (
            id              TEXT PRIMARY KEY,
            status          TEXT NOT NULL DEFAULT 'queued',
            total           INT NOT NULL DEFAULT 0,
            done            INT NOT NULL DEFAULT 0,
            kept            INT NOT NULL DEFAULT 0,
            deleted         INT NOT NULL DEFAULT 0,
            skipped         INT NOT NULL DEFAULT 0,
            missing         INT NOT NULL DEFAULT 0,
            current_label   TEXT,
            message         TEXT,
            stop_requested  BOOLEAN NOT NULL DEFAULT FALSE,
            plan_filter     TEXT,
            q_filter        TEXT,
            created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            finished_at     TIMESTAMPTZ
        );
        CREATE INDEX IF NOT EXISTS idx_recheck_jobs_status
            ON recheck_jobs (status);
        CREATE INDEX IF NOT EXISTS idx_recheck_jobs_created
            ON recheck_jobs (created_at DESC);
        """
        with db_conn() as conn:
            with conn:
                with conn.cursor() as cur:
                    cur.execute(sql)
        _tables_ready = True


def _pct(done: int, total: int) -> int:
    if not total:
        return 0
    return int(round(100 * done / total))


def _update_job(job_id: str, *, write_db: bool = True, finished: bool = False, **fields: Any) -> None:
    allowed = {
        "status",
        "total",
        "done",
        "kept",
        "deleted",
        "skipped",
        "missing",
        "current_label",
        "message",
    }
    with _lock:
        m = _mem.setdefault(job_id, {"id": job_id})
        for k, v in fields.items():
            if k in allowed:
                m[k] = v
        m["percent"] = _pct(int(m.get("done") or 0), int(m.get("total") or 0))

    if not write_db:
        return

    sets = []
    vals: List[Any] = []
    for k, v in fields.items():
        if k in allowed:
            sets.append(f"{k} = %s")
            vals.append(v)
    extra = ", finished_at = NOW()" if finished else ""
    if not sets and not finished:
        return
    if sets:
        sql = f"UPDATE recheck_jobs SET {', '.join(sets)}, updated_at = NOW(){extra} WHERE id = %s"
        vals.append(job_id)
    else:
        sql = f"UPDATE recheck_jobs SET updated_at = NOW(){extra} WHERE id = %s"
        vals = [job_id]
    with db_conn() as conn:
        with conn:
            with conn.cursor() as cur:
                cur.execute(sql, vals)


def request_stop(job_id: str) -> None:
    with _lock:
        _stop[job_id] = True
        if job_id in _mem:
            _mem[job_id]["stop_requested"] = True
    try:
        ensure_recheck_tables()
        with db_conn() as conn:
            with conn:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        UPDATE recheck_jobs
                        SET stop_requested = TRUE,
                            message = CASE
                                WHEN COALESCE(message,'') LIKE '%stop%' THEN message
                                ELSE COALESCE(message,'') || ' · stop requested'
                            END,
                            updated_at = NOW()
                        WHERE id = %s AND status IN ('running', 'queued')
                        """,
                        (job_id,),
                    )
    except Exception:
        pass


def is_stop_requested(job_id: str, force_db: bool = False) -> bool:
    with _lock:
        if _stop.get(job_id):
            return True
    if not force_db:
        return False
    try:
        with db_conn(role="worker", timeout_sec=3.0) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT stop_requested FROM recheck_jobs WHERE id = %s",
                    (job_id,),
                )
                row = cur.fetchone()
                if row and row[0]:
                    with _lock:
                        _stop[job_id] = True
                    return True
    except Exception:
        pass
    return False


def get_job(job_id: str) -> Optional[Dict[str, Any]]:
    ensure_recheck_tables()
    mem = None
    with _lock:
        if job_id in _mem:
            mem = dict(_mem[job_id])

    with db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, status, total, done, kept, deleted, skipped, missing,
                       current_label, message, stop_requested,
                       plan_filter, q_filter, created_at, updated_at, finished_at
                FROM recheck_jobs WHERE id = %s
                """,
                (job_id,),
            )
            row = cur.fetchone()
            if not row:
                return mem
            keys = [
                "id",
                "status",
                "total",
                "done",
                "kept",
                "deleted",
                "skipped",
                "missing",
                "current_label",
                "message",
                "stop_requested",
                "plan_filter",
                "q_filter",
                "created_at",
                "updated_at",
                "finished_at",
            ]
            data = dict(zip(keys, row))
            for k in ("created_at", "updated_at", "finished_at"):
                if data.get(k) is not None and hasattr(data[k], "isoformat"):
                    data[k] = data[k].isoformat()

    if mem and int(mem.get("done") or 0) > int(data.get("done") or 0):
        out = dict(data)
        for k in (
            "status",
            "total",
            "done",
            "kept",
            "deleted",
            "skipped",
            "missing",
            "current_label",
            "message",
        ):
            if k in mem and mem[k] is not None:
                out[k] = mem[k]
        out["percent"] = _pct(int(out.get("done") or 0), int(out.get("total") or 0))
        return out

    data["percent"] = _pct(int(data.get("done") or 0), int(data.get("total") or 0))
    return data


def get_active_job() -> Optional[Dict[str, Any]]:
    ensure_recheck_tables()
    with db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id FROM recheck_jobs
                WHERE status IN ('running', 'queued')
                ORDER BY created_at DESC
                LIMIT 1
                """
            )
            row = cur.fetchone()
            if not row:
                return None
            return get_job(str(row[0]))


def find_running_job_id() -> Optional[str]:
    job = get_active_job()
    return str(job["id"]) if job else None


def process_recheck_job(
    job_id: str,
    ids: List[int],
    timeout: int = DEFAULT_TIMEOUT,
    delete_on_error: bool = False,
) -> None:
    with use_worker_db():
        try:
            ensure_recheck_tables()
            timeout = max(5, min(int(timeout or DEFAULT_TIMEOUT), 25))
            total = len(ids)
            _update_job(
                job_id,
                status="running",
                total=total,
                done=0,
                kept=0,
                deleted=0,
                skipped=0,
                missing=0,
                message=f"Recheck nền · {total} TK · timeout {timeout}s",
            )
            if total == 0:
                _update_job(
                    job_id,
                    status="done",
                    message="Không có TK để recheck",
                    finished=True,
                )
                return

            kept = deleted = skipped = missing = done = 0
            last_stop = 0.0

            def stop_now() -> bool:
                nonlocal last_stop
                now = time.time()
                if now - last_stop >= 1.5:
                    last_stop = now
                    return is_stop_requested(job_id, force_db=True)
                return is_stop_requested(job_id, force_db=False)

            for i in range(0, total, CHUNK):
                if stop_now():
                    _update_job(
                        job_id,
                        status="stopped",
                        done=done,
                        kept=kept,
                        deleted=deleted,
                        skipped=skipped,
                        missing=missing,
                        message=f"Admin dừng · {done}/{total}",
                        finished=True,
                    )
                    return

                chunk = ids[i : i + CHUNK]
                summary = recheck_live_cookie_ids(
                    chunk,
                    timeout=timeout,
                    delete_on_error=delete_on_error,
                    max_ids=len(chunk),
                )
                kept += int(summary.get("kept") or 0)
                deleted += int(summary.get("deleted") or 0)
                skipped += int(summary.get("skipped") or 0)
                missing += int(summary.get("missing") or 0)
                done = min(total, i + len(chunk))
                last_label = None
                results = summary.get("results") or []
                if results:
                    last = results[-1]
                    last_label = last.get("email") or last.get("filename") or last.get("id")

                _update_job(
                    job_id,
                    done=done,
                    kept=kept,
                    deleted=deleted,
                    skipped=skipped,
                    missing=missing,
                    current_label=str(last_label) if last_label is not None else None,
                    message=(
                        f"[{done}/{total}] giữ {kept} · xóa {deleted} · "
                        f"skip {skipped} · {last_label or ''}"
                    ),
                )
                if YIELD_SEC > 0:
                    time.sleep(YIELD_SEC)

            _update_job(
                job_id,
                status="done",
                done=total,
                kept=kept,
                deleted=deleted,
                skipped=skipped,
                missing=missing,
                current_label=None,
                message=(
                    f"Xong. giữ LIVE {kept} · xóa {deleted} · "
                    f"bỏ qua {skipped} · thiếu {missing}"
                ),
                finished=True,
            )
        except Exception as exc:
            try:
                _update_job(
                    job_id,
                    status="error",
                    message=f"Job lỗi: {exc}",
                    finished=True,
                )
            except Exception:
                pass
            traceback.print_exc()


def _entry(
    job_id: str,
    ids: List[int],
    timeout: int,
    delete_on_error: bool,
) -> None:
    try:
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
        process_recheck_job(job_id, ids, timeout=timeout, delete_on_error=delete_on_error)
    except Exception:
        traceback.print_exc()


def start_recheck_job(
    ids: Optional[List[int]] = None,
    *,
    plan: str = "",
    q: str = "",
    timeout: int = DEFAULT_TIMEOUT,
    delete_on_error: bool = False,
    all_live: bool = False,
) -> Dict[str, Any]:
    """
    Start background recheck. Provide ids, or all_live=True with optional plan/q filters.
    """
    if not is_db_configured():
        raise RuntimeError("DATABASE_URL not configured")
    ensure_recheck_tables()

    other = find_running_job_id()
    if other:
        raise RuntimeError(
            f"Đang có job recheck chạy ({other[:8]}…). Đợi xong hoặc Stop trước."
        )

    clean: List[int] = []
    if all_live or not ids:
        data = list_live_cookie_ids(plan=plan, status="LIVE", q=q)
        clean = list(data.get("ids") or [])
    else:
        for raw in ids or []:
            try:
                clean.append(int(raw))
            except Exception:
                continue
        clean = list(dict.fromkeys(clean))

    if not clean:
        raise RuntimeError("Không có TK LIVE để recheck")

    job_id = secrets.token_urlsafe(10)
    msg = f"Queued · {len(clean)} TK · tắt web vẫn chạy trên BE"
    with db_conn() as conn:
        with conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO recheck_jobs (
                        id, status, total, done, kept, deleted, skipped, missing,
                        message, stop_requested, plan_filter, q_filter
                    ) VALUES (%s, 'queued', %s, 0, 0, 0, 0, 0, %s, FALSE, %s, %s)
                    """,
                    (job_id, len(clean), msg, plan or None, q or None),
                )

    with _lock:
        _stop[job_id] = False
        _mem[job_id] = {
            "id": job_id,
            "status": "queued",
            "total": len(clean),
            "done": 0,
            "kept": 0,
            "deleted": 0,
            "skipped": 0,
            "missing": 0,
            "percent": 0,
            "message": msg,
        }

    mode = "thread"
    if USE_PROCESS:
        try:
            ctx = multiprocessing.get_context("spawn")
            p = ctx.Process(
                target=_entry,
                args=(job_id, clean, timeout, delete_on_error),
                name=f"recheck-{job_id[:8]}",
                daemon=True,
            )
            p.start()
            mode = "process"
        except Exception as exc:
            print(f"[recheck] process start failed ({exc}); fallback thread")
            t = threading.Thread(
                target=_entry,
                args=(job_id, clean, timeout, delete_on_error),
                name=f"recheck-{job_id[:8]}",
                daemon=True,
            )
            t.start()
    else:
        t = threading.Thread(
            target=_entry,
            args=(job_id, clean, timeout, delete_on_error),
            name=f"recheck-{job_id[:8]}",
            daemon=True,
        )
        t.start()

    return {
        "job": get_job(job_id),
        "runner": mode,
        "total": len(clean),
    }


def mark_stale_recheck_jobs() -> int:
    if not is_db_configured():
        return 0
    try:
        ensure_recheck_tables()
        with db_conn() as conn:
            with conn:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        UPDATE recheck_jobs
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
