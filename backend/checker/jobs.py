"""Server-side background jobs: upload → save PENDING → check → delete dead."""

from __future__ import annotations

import os
import secrets
import threading
import traceback
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from .db import (
    db_conn,
    delete_cookies_by_ids,
    get_connection,
    init_db,
    is_db_configured,
    put_connection,
)
from .service import check_cookie_content, quick_status_from_result

# Parallel Netflix checks (override with CHECK_WORKERS env)
DEFAULT_WORKERS = max(1, min(12, int(os.environ.get("CHECK_WORKERS") or 6)))
DEFAULT_TIMEOUT = max(5, int(os.environ.get("CHECK_TIMEOUT") or 10))

# job_id -> stop requested
_stop_flags: Dict[str, bool] = {}
_lock = threading.Lock()
# in-memory cache for fast poll (also mirrored in DB)
_jobs: Dict[str, Dict[str, Any]] = {}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _job_snapshot(job_id: str) -> Optional[Dict[str, Any]]:
    with _lock:
        j = _jobs.get(job_id)
        return dict(j) if j else None


def request_stop(job_id: str) -> None:
    with _lock:
        _stop_flags[job_id] = True
        if job_id in _jobs:
            _jobs[job_id]["stop_requested"] = True


def is_stop_requested(job_id: str) -> bool:
    with _lock:
        return bool(_stop_flags.get(job_id))


def ensure_job_tables() -> None:
    init_db()
    sql = """
    ALTER TABLE netflix_live_cookies
        ADD COLUMN IF NOT EXISTS job_id TEXT;
    ALTER TABLE netflix_live_cookies
        ADD COLUMN IF NOT EXISTS message TEXT;

    CREATE INDEX IF NOT EXISTS idx_netflix_live_cookies_job_id
        ON netflix_live_cookies (job_id);

    CREATE TABLE IF NOT EXISTS netflix_check_jobs (
        id              TEXT PRIMARY KEY,
        status          TEXT NOT NULL DEFAULT 'queued',
        total           INT NOT NULL DEFAULT 0,
        done            INT NOT NULL DEFAULT 0,
        kept            INT NOT NULL DEFAULT 0,
        deleted         INT NOT NULL DEFAULT 0,
        failed          INT NOT NULL DEFAULT 0,
        current_file    TEXT,
        message         TEXT,
        created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        finished_at     TIMESTAMPTZ
    );
    """
    conn = get_connection()
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute(sql)
    finally:
        put_connection(conn)


def create_job_record(job_id: str, total: int) -> None:
    ensure_job_tables()
    conn = get_connection()
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO netflix_check_jobs (id, status, total, done, kept, deleted, failed, message)
                    VALUES (%s, 'queued', %s, 0, 0, 0, 0, %s)
                    ON CONFLICT (id) DO UPDATE SET
                        status = 'queued',
                        total = EXCLUDED.total,
                        done = 0,
                        kept = 0,
                        deleted = 0,
                        failed = 0,
                        message = EXCLUDED.message,
                        updated_at = NOW(),
                        finished_at = NULL
                    """,
                    (job_id, total, f"Queued {total} cookies"),
                )
    finally:
        put_connection(conn)

    with _lock:
        _jobs[job_id] = {
            "id": job_id,
            "status": "queued",
            "total": total,
            "done": 0,
            "kept": 0,
            "deleted": 0,
            "failed": 0,
            "current_file": None,
            "message": f"Queued {total} cookies",
            "stop_requested": False,
        }
        _stop_flags[job_id] = False


def update_job(
    job_id: str,
    *,
    status: Optional[str] = None,
    total: Optional[int] = None,
    done: Optional[int] = None,
    kept: Optional[int] = None,
    deleted: Optional[int] = None,
    failed: Optional[int] = None,
    current_file: Optional[str] = None,
    message: Optional[str] = None,
    finished: bool = False,
) -> Dict[str, Any]:
    with _lock:
        j = _jobs.setdefault(job_id, {"id": job_id})
        if status is not None:
            j["status"] = status
        if total is not None:
            j["total"] = total
        if done is not None:
            j["done"] = done
        if kept is not None:
            j["kept"] = kept
        if deleted is not None:
            j["deleted"] = deleted
        if failed is not None:
            j["failed"] = failed
        if current_file is not None:
            j["current_file"] = current_file
        if message is not None:
            j["message"] = message
        t = int(j.get("total") or 0)
        d = int(j.get("done") or 0)
        j["percent"] = int(round(100 * d / t)) if t else 0
        snap = dict(j)

    conn = get_connection()
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE netflix_check_jobs SET
                        status = COALESCE(%s, status),
                        total = COALESCE(%s, total),
                        done = COALESCE(%s, done),
                        kept = COALESCE(%s, kept),
                        deleted = COALESCE(%s, deleted),
                        failed = COALESCE(%s, failed),
                        current_file = COALESCE(%s, current_file),
                        message = COALESCE(%s, message),
                        updated_at = NOW(),
                        finished_at = CASE WHEN %s THEN NOW() ELSE finished_at END
                    WHERE id = %s
                    """,
                    (
                        status,
                        total,
                        done,
                        kept,
                        deleted,
                        failed,
                        current_file,
                        message,
                        finished,
                        job_id,
                    ),
                )
    finally:
        put_connection(conn)
    return snap


def get_job(job_id: str) -> Optional[Dict[str, Any]]:
    """Prefer live in-memory progress (fast), else DB (survives new browser session)."""
    mem = _job_snapshot(job_id)
    db_data = None
    if is_db_configured():
        try:
            ensure_job_tables()
            conn = get_connection()
            try:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        SELECT id, status, total, done, kept, deleted, failed,
                               current_file, message, created_at, updated_at, finished_at
                        FROM netflix_check_jobs WHERE id = %s
                        """,
                        (job_id,),
                    )
                    row = cur.fetchone()
                    if row:
                        keys = [
                            "id",
                            "status",
                            "total",
                            "done",
                            "kept",
                            "deleted",
                            "failed",
                            "current_file",
                            "message",
                            "created_at",
                            "updated_at",
                            "finished_at",
                        ]
                        db_data = dict(zip(keys, row))
                        for k in ("created_at", "updated_at", "finished_at"):
                            if db_data.get(k) is not None and hasattr(db_data[k], "isoformat"):
                                db_data[k] = db_data[k].isoformat()
                        total = int(db_data.get("total") or 0)
                        done = int(db_data.get("done") or 0)
                        db_data["percent"] = int(round(100 * done / total)) if total else 0
            finally:
                put_connection(conn)
        except Exception:
            db_data = None

    # If worker is alive in this process, memory is freshest for running jobs
    if mem and str(mem.get("status") or "").lower() in {"running", "queued"}:
        out = dict(mem)
        total = int(out.get("total") or 0)
        done = int(out.get("done") or 0)
        out["percent"] = int(round(100 * done / total)) if total else 0
        return out

    if db_data:
        return db_data
    return mem


def list_recent_jobs(limit: int = 20) -> List[Dict[str, Any]]:
    ensure_job_tables()
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, status, total, done, kept, deleted, failed,
                       current_file, message, created_at, updated_at, finished_at
                FROM netflix_check_jobs
                ORDER BY created_at DESC
                LIMIT %s
                """,
                (max(1, min(limit, 50)),),
            )
            keys = [
                "id",
                "status",
                "total",
                "done",
                "kept",
                "deleted",
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
                total = int(data.get("total") or 0)
                done = int(data.get("done") or 0)
                data["percent"] = int(round(100 * done / total)) if total else 0
                out.append(data)
            return out
    finally:
        put_connection(conn)


def insert_pending_cookies(job_id: str, items: List[Dict[str, str]]) -> Dict[str, Any]:
    """
    items: [{filename, cookie_content}, ...]
    Saves all as PENDING first. Returns {inserted, updated, skipped, ids}
    """
    from .db import content_hash, extract_netscape_for_storage

    ensure_job_tables()
    inserted = updated = skipped = 0
    ids: List[int] = []
    now = _now()

    conn = get_connection()
    try:
        with conn:
            with conn.cursor() as cur:
                for item in items:
                    filename = item.get("filename") or "cookie.txt"
                    raw = item.get("cookie_content") or ""
                    content = extract_netscape_for_storage(raw)
                    if not content:
                        skipped += 1
                        continue
                    chash = content_hash(content)
                    cur.execute(
                        """
                        INSERT INTO netflix_live_cookies (
                            filename, email, plan, country, status, status_label,
                            cookie_content, content_hash, source, job_id, message,
                            checked_at, created_at, updated_at
                        ) VALUES (
                            %s, NULL, NULL, NULL, 'PENDING', 'PENDING · waiting',
                            %s, %s, 'admin_job', %s, %s,
                            %s, %s, %s
                        )
                        ON CONFLICT (content_hash) DO UPDATE SET
                            filename = EXCLUDED.filename,
                            status = 'PENDING',
                            status_label = 'PENDING · re-check',
                            cookie_content = EXCLUDED.cookie_content,
                            job_id = EXCLUDED.job_id,
                            message = EXCLUDED.message,
                            source = 'admin_job',
                            updated_at = EXCLUDED.updated_at
                        RETURNING id, (xmax = 0) AS is_insert
                        """,
                        (
                            filename,
                            content,
                            chash,
                            job_id,
                            "Queued for check",
                            now,
                            now,
                            now,
                        ),
                    )
                    row = cur.fetchone()
                    if row:
                        ids.append(int(row[0]))
                        if row[1]:
                            inserted += 1
                        else:
                            updated += 1
                    else:
                        skipped += 1
    finally:
        put_connection(conn)

    return {
        "inserted": inserted,
        "updated": updated,
        "skipped": skipped,
        "ids": ids,
        "total": len(ids),
    }


def fetch_job_cookie_rows(job_id: str) -> List[Dict[str, Any]]:
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, filename, cookie_content, status
                FROM netflix_live_cookies
                WHERE job_id = %s
                ORDER BY id ASC
                """,
                (job_id,),
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
    finally:
        put_connection(conn)


def mark_cookie_checking(row_id: int) -> None:
    conn = get_connection()
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE netflix_live_cookies
                    SET status = 'CHECKING', status_label = 'CHECKING...',
                        message = 'Checking with Netflix', updated_at = NOW()
                    WHERE id = %s
                    """,
                    (row_id,),
                )
    finally:
        put_connection(conn)


def keep_cookie_live(row_id: int, status_row: Dict[str, Any]) -> None:
    conn = get_connection()
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE netflix_live_cookies SET
                        email = %s,
                        plan = %s,
                        country = %s,
                        status = %s,
                        status_label = %s,
                        message = %s,
                        checked_at = NOW(),
                        updated_at = NOW()
                    WHERE id = %s
                    """,
                    (
                        status_row.get("email"),
                        status_row.get("plan"),
                        status_row.get("country"),
                        status_row.get("status") or "LIVE",
                        status_row.get("status_label"),
                        status_row.get("message") or "Kept",
                        row_id,
                    ),
                )
    finally:
        put_connection(conn)


def should_keep_status_row(row: Dict[str, Any]) -> bool:
    """Keep only real LIVE (subscribed, not on-hold). Drop HOLD/FREE/DEAD."""
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

    # HOLD = billing hold — discard when filtering job
    if status == "HOLD":
        return False
    if status == "LIVE":
        return (not blank(email)) or (not blank(country))
    # FREE and everything else — do not keep
    return False


def _check_one_row(row: Dict[str, Any], timeout: int) -> Dict[str, Any]:
    """Worker: check one cookie. Returns action summary (no job progress update)."""
    row_id = row["id"]
    filename = row.get("filename") or f"#{row_id}"
    content = row.get("cookie_content") or ""
    try:
        mark_cookie_checking(row_id)
        # Job path: no fallback page (1 Netflix request instead of 2) = faster
        result = check_cookie_content(
            content,
            make_nftoken=False,
            timeout=timeout,
            fallback=False,
        )
        status_row = quick_status_from_result(result, filename=filename)
        if should_keep_status_row(status_row):
            keep_cookie_live(row_id, status_row)
            return {
                "filename": filename,
                "action": "kept",
                "status": status_row.get("status"),
            }
        delete_cookies_by_ids([row_id])
        return {
            "filename": filename,
            "action": "deleted",
            "status": status_row.get("status") or "DEAD",
            "message": status_row.get("message"),
        }
    except Exception as exc:
        try:
            delete_cookies_by_ids([row_id])
        except Exception:
            pass
        return {"filename": filename, "action": "failed", "message": str(exc)}


def process_job(
    job_id: str,
    timeout: int = DEFAULT_TIMEOUT,
    workers: int = DEFAULT_WORKERS,
) -> None:
    """Background worker: parallel check cookies; delete dead, keep live."""
    try:
        workers = max(1, min(int(workers or DEFAULT_WORKERS), 16))
        timeout = max(5, int(timeout or DEFAULT_TIMEOUT))
        update_job(
            job_id,
            status="running",
            message=f"Job started · {workers} workers · timeout {timeout}s",
        )
        rows = fetch_job_cookie_rows(job_id)
        total = len(rows)
        update_job(
            job_id,
            total=total,
            done=0,
            message=f"Checking {total} cookies · parallel x{workers}",
        )

        if total == 0:
            update_job(
                job_id,
                status="done",
                message="No cookies to check",
                finished=True,
            )
            return

        kept = deleted = failed = 0
        done = 0
        progress_lock = threading.Lock()

        def on_result(res: Dict[str, Any]) -> None:
            nonlocal kept, deleted, failed, done
            with progress_lock:
                action = res.get("action")
                if action == "kept":
                    kept += 1
                elif action == "deleted":
                    deleted += 1
                else:
                    failed += 1
                    deleted += 1  # failed rows deleted
                done += 1
                update_job(
                    job_id,
                    done=done,
                    kept=kept,
                    deleted=deleted,
                    failed=failed,
                    current_file=res.get("filename"),
                    message=(
                        f"[{done}/{total}] {res.get('filename')} → {res.get('action')} "
                        f"(workers={workers})"
                    ),
                )

        # Submit in chunks so stop can take effect between batches
        batch_size = max(workers * 2, workers)
        idx = 0
        while idx < total:
            if is_stop_requested(job_id):
                update_job(
                    job_id,
                    status="stopped",
                    done=done,
                    kept=kept,
                    deleted=deleted,
                    failed=failed,
                    message=f"Stopped by admin at {done}/{total}",
                    finished=True,
                )
                return

            batch = rows[idx : idx + batch_size]
            idx += len(batch)
            with ThreadPoolExecutor(max_workers=workers) as pool:
                futures = [
                    pool.submit(_check_one_row, row, timeout) for row in batch
                ]
                for fut in as_completed(futures):
                    if is_stop_requested(job_id):
                        # cancel remaining not started; running will finish
                        pass
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

            if is_stop_requested(job_id):
                update_job(
                    job_id,
                    status="stopped",
                    done=done,
                    kept=kept,
                    deleted=deleted,
                    failed=failed,
                    message=f"Stopped by admin at {done}/{total}",
                    finished=True,
                )
                return

        update_job(
            job_id,
            status="done",
            done=total,
            kept=kept,
            deleted=deleted,
            failed=failed,
            current_file=None,
            message=(
                f"Done in parallel x{workers}. Kept {kept}, deleted {deleted}, errors {failed}"
            ),
            finished=True,
        )
    except Exception as exc:
        update_job(
            job_id,
            status="error",
            message=f"Job failed: {exc}",
            finished=True,
        )
        traceback.print_exc()


def start_job_thread(
    job_id: str,
    timeout: int = DEFAULT_TIMEOUT,
    workers: int = DEFAULT_WORKERS,
) -> None:
    t = threading.Thread(
        target=process_job,
        args=(job_id, timeout, workers),
        name=f"cookie-job-{job_id[:8]}",
        daemon=True,
    )
    t.start()


def mark_stale_running_jobs() -> int:
    """After server restart, old 'running' jobs have dead threads — mark error."""
    if not is_db_configured():
        return 0
    try:
        ensure_job_tables()
        conn = get_connection()
        try:
            with conn:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        UPDATE netflix_check_jobs
                        SET status = 'error',
                            message = COALESCE(message, '') || ' · server restarted (job interrupted)',
                            finished_at = NOW(),
                            updated_at = NOW()
                        WHERE status IN ('running', 'queued')
                          AND finished_at IS NULL
                        RETURNING id
                        """
                    )
                    rows = cur.fetchall() or []
                    return len(rows)
        finally:
            put_connection(conn)
    except Exception:
        return 0


def create_and_start_job(
    items: List[Dict[str, str]],
    timeout: int = DEFAULT_TIMEOUT,
    workers: int = DEFAULT_WORKERS,
) -> Dict[str, Any]:
    """
    Full pipeline:
      1) create job
      2) insert all cookies as PENDING
      3) start background check (delete dead, keep live)
    """
    if not is_db_configured():
        raise RuntimeError("DATABASE_URL not configured")

    job_id = secrets.token_urlsafe(12)
    # pre-create with 0, then update total after insert
    create_job_record(job_id, total=len(items))
    saved = insert_pending_cookies(job_id, items)
    total = int(saved.get("total") or 0)
    update_job(
        job_id,
        total=total,
        message=f"Saved {total} PENDING · start check x{workers} workers",
    )

    if total == 0:
        update_job(
            job_id,
            status="done",
            message="No valid cookie content to process",
            finished=True,
        )
        return {"job_id": job_id, "saved": saved, "job": get_job(job_id)}

    start_job_thread(job_id, timeout=timeout, workers=workers)
    return {
        "job_id": job_id,
        "saved": saved,
        "workers": workers,
        "timeout": timeout,
        "job": get_job(job_id),
    }
