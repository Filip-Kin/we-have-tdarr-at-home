"""auto-transcode: FastAPI app + background worker + SQLite state."""

from __future__ import annotations

import fnmatch
import logging
import os
import queue
import shutil
import sqlite3
import sys
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

import transcode

# -------------------- config --------------------

LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")
DB_PATH = Path(os.getenv("DB_PATH", "/data/auto-transcode.db"))
MEDIA_ROOTS = [Path(p.strip()) for p in os.getenv("MEDIA_ROOTS", "").split(",") if p.strip()]
ALLOWLIST_GLOB = os.getenv("ALLOWLIST_GLOB", "").strip()
WATCHER_ENABLED = os.getenv("WATCHER_ENABLED", "false").lower() == "true"
HOLD_FOR_APPROVAL = os.getenv("HOLD_FOR_APPROVAL", "true").lower() == "true"
KEEP_ORIGINAL_BACKUP = os.getenv("KEEP_ORIGINAL_BACKUP", "true").lower() == "true"
X265_PRESET = os.getenv("X265_PRESET", "fast")
X265_CRF = int(os.getenv("X265_CRF", "22"))

logging.basicConfig(
    level=LOG_LEVEL,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    stream=sys.stdout,
)
log = logging.getLogger("auto-transcode")

# -------------------- db --------------------

_db_lock = threading.Lock()
_conn: sqlite3.Connection | None = None


def db() -> sqlite3.Connection:
    global _conn
    if _conn is None:
        DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        _conn = sqlite3.connect(str(DB_PATH), check_same_thread=False, isolation_level=None)
        _conn.row_factory = sqlite3.Row
        _conn.execute("PRAGMA journal_mode=WAL")
        _conn.execute("PRAGMA synchronous=NORMAL")
        _init_schema(_conn)
    return _conn


def _init_schema(c: sqlite3.Connection) -> None:
    c.executescript("""
    CREATE TABLE IF NOT EXISTS jobs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        src_path TEXT NOT NULL,
        dst_path TEXT,
        backup_path TEXT,
        status TEXT NOT NULL,
        reason TEXT,
        src_size INTEGER,
        dst_size INTEGER,
        duration_sec REAL,
        height INTEGER,
        src_codec TEXT,
        src_bitrate_kbps INTEGER,
        ffmpeg_cmd TEXT,
        error_msg TEXT,
        created_at REAL NOT NULL,
        started_at REAL,
        finished_at REAL,
        approved_at REAL,
        progress_frame INTEGER DEFAULT 0,
        progress_fps REAL DEFAULT 0,
        progress_speed REAL DEFAULT 0,
        progress_pct REAL DEFAULT 0,
        progress_eta_sec INTEGER DEFAULT 0,
        progress_size INTEGER DEFAULT 0
    );
    CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status);
    CREATE INDEX IF NOT EXISTS idx_jobs_src ON jobs(src_path);
    """)


@contextmanager
def db_lock():
    with _db_lock:
        yield db()


# -------------------- state model --------------------

# Statuses:
#   queued, running, verifying, pending_approval, done, failed, discarded

def insert_job(src: Path, reason: str) -> int:
    with db_lock() as c:
        cur = c.execute("""
            INSERT INTO jobs (src_path, status, reason, src_size, created_at)
            VALUES (?, 'queued', ?, ?, ?)
        """, (str(src), reason, src.stat().st_size, time.time()))
        return cur.lastrowid


def update_job(job_id: int, **fields: Any) -> None:
    if not fields:
        return
    cols = ", ".join(f"{k}=?" for k in fields)
    vals = list(fields.values()) + [job_id]
    with db_lock() as c:
        c.execute(f"UPDATE jobs SET {cols} WHERE id=?", vals)


def get_job(job_id: int) -> sqlite3.Row | None:
    with db_lock() as c:
        return c.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()


def jobs_by_status(*statuses: str) -> list[sqlite3.Row]:
    placeholders = ",".join("?" * len(statuses))
    with db_lock() as c:
        return c.execute(
            f"SELECT * FROM jobs WHERE status IN ({placeholders}) ORDER BY id DESC",
            statuses,
        ).fetchall()


def has_open_job_for(src_path: str) -> bool:
    """True if there's an existing non-terminal job for this source path."""
    with db_lock() as c:
        row = c.execute("""
            SELECT 1 FROM jobs
            WHERE src_path=? AND status IN ('queued','running','verifying','pending_approval')
            LIMIT 1
        """, (src_path,)).fetchone()
        return row is not None


def total_savings_bytes() -> int:
    with db_lock() as c:
        row = c.execute("""
            SELECT COALESCE(SUM(src_size - dst_size), 0) AS saved
            FROM jobs WHERE status='done' AND dst_size IS NOT NULL
        """).fetchone()
        return int(row["saved"] or 0)


# -------------------- worker --------------------

_worker_queue: queue.Queue[int] = queue.Queue()
_worker_thread: threading.Thread | None = None
_active_job_id: int | None = None
_active_lock = threading.Lock()


def _allowed(path: Path) -> bool:
    if not ALLOWLIST_GLOB:
        return True
    # fnmatch is glob-style: "**/Network (1976)/*.mkv"
    # but fnmatch doesn't understand **; manual match: substring of path
    spec = ALLOWLIST_GLOB
    return fnmatch.fnmatch(str(path), spec) or spec.replace("**/", "") in str(path)


def enqueue_path(src: Path, reason: str = "manual") -> int | None:
    if not src.exists():
        raise FileNotFoundError(src)
    if not _allowed(src):
        log.warning("enqueue rejected by allowlist: %s", src)
        return None
    if has_open_job_for(str(src)):
        log.info("already an open job for %s", src)
        return None
    jid = insert_job(src, reason)
    _worker_queue.put(jid)
    log.info("enqueued job %d: %s", jid, src)
    return jid


def scan_and_enqueue() -> dict[str, int]:
    """Walk MEDIA_ROOTS, probe each video, enqueue if needs transcoding.
    Enqueues biggest files first so the slowest jobs run early."""
    enqueued = 0
    skipped = 0
    errors = 0
    candidates: list[tuple[int, Path]] = []
    for root in MEDIA_ROOTS:
        if not root.exists():
            log.warning("media root missing: %s", root)
            continue
        for p in root.rglob("*"):
            if not p.is_file():
                continue
            if p.suffix.lower() not in transcode.VIDEO_EXTS:
                continue
            if not _allowed(p):
                continue
            if has_open_job_for(str(p)):
                continue
            try:
                candidates.append((p.stat().st_size, p))
            except OSError as e:
                log.warning("stat failed for %s: %s", p, e)
                errors += 1
    candidates.sort(key=lambda x: x[0], reverse=True)
    log.info("scan: %d candidate files, probing biggest first", len(candidates))
    for _, p in candidates:
        try:
            probed = transcode.probe(p)
        except Exception as e:
            log.warning("probe failed for %s: %s", p, e)
            errors += 1
            continue
        needs, reason = transcode.needs_transcode(probed)
        if needs:
            enqueue_path(p, reason=reason)
            enqueued += 1
        else:
            skipped += 1
    log.info("scan complete: enqueued=%d skipped=%d errors=%d",
             enqueued, skipped, errors)
    return {"enqueued": enqueued, "skipped": skipped, "errors": errors}


def _worker_loop() -> None:
    log.info("worker thread started")
    while True:
        try:
            job_id = _worker_queue.get()
        except Exception:
            time.sleep(1)
            continue
        try:
            _process_job(job_id)
        except Exception:
            log.exception("worker crash on job %d", job_id)
            update_job(job_id, status="failed",
                       error_msg="worker exception (see logs)",
                       finished_at=time.time())
        finally:
            with _active_lock:
                globals()["_active_job_id"] = None


def _process_job(job_id: int) -> None:
    row = get_job(job_id)
    if not row:
        log.error("job %d disappeared", job_id)
        return
    src = Path(row["src_path"])
    if not src.exists():
        update_job(job_id, status="failed", error_msg="source missing",
                   finished_at=time.time())
        return

    with _active_lock:
        globals()["_active_job_id"] = job_id

    log.info("[job %d] starting %s", job_id, src)
    try:
        probed = transcode.probe(src)
    except Exception as e:
        update_job(job_id, status="failed", error_msg=f"probe: {e}",
                   finished_at=time.time())
        return

    update_job(
        job_id,
        status="running",
        started_at=time.time(),
        duration_sec=probed.duration_sec,
        height=probed.height,
        src_codec=probed.video_codec,
        src_bitrate_kbps=probed.video_bitrate_bps // 1000,
    )

    dst = src.with_suffix(".transcoding.mkv")
    if dst.exists():
        log.warning("[job %d] leftover temp file, removing: %s", job_id, dst)
        try:
            dst.unlink()
        except OSError as e:
            update_job(job_id, status="failed", error_msg=f"cleanup: {e}",
                       finished_at=time.time())
            return

    cmd = transcode.build_ffmpeg_cmd(probed, dst, preset=X265_PRESET, crf=X265_CRF)
    update_job(job_id, dst_path=str(dst), ffmpeg_cmd=" ".join(cmd))

    total_frames = probed.total_frames
    duration_sec = probed.duration_sec
    last_db_update = 0.0
    last_stderr: list[str] = []

    def on_progress(u: transcode.ProgressUpdate) -> None:
        nonlocal last_db_update
        # Frame-based pct is more reliable than out_time_us, which ffmpeg 7.x
        # often reports as "N/A" for the whole run on multi-stream encodes.
        pct = min(100.0, 100.0 * u.frame / total_frames)
        eta = 0
        if u.speed > 0 and u.frame < total_frames:
            remaining_frames = total_frames - u.frame
            remaining_content_sec = remaining_frames / probed.fps
            eta = int(remaining_content_sec / u.speed)
        now = time.time()
        # Throttle DB writes to once per ~3 seconds; on done, always write.
        if u.done or now - last_db_update > 3:
            update_job(
                job_id,
                progress_frame=u.frame,
                progress_fps=u.fps,
                progress_speed=u.speed,
                progress_pct=pct,
                progress_eta_sec=eta,
                progress_size=u.total_size,
            )
            last_db_update = now

    def on_stderr(line: str) -> None:
        last_stderr.append(line)
        if len(last_stderr) > 200:
            last_stderr.pop(0)
        if "error" in line.lower() or "failed" in line.lower():
            log.warning("[job %d] ffmpeg: %s", job_id, line)

    rc = transcode.run_ffmpeg(cmd, on_progress=on_progress, on_stderr_line=on_stderr)

    if rc != 0:
        log.error("[job %d] ffmpeg rc=%d", job_id, rc)
        update_job(
            job_id,
            status="failed",
            error_msg=f"ffmpeg rc={rc}: " + " | ".join(last_stderr[-5:]),
            finished_at=time.time(),
        )
        try:
            dst.unlink(missing_ok=True)
        except OSError:
            pass
        return

    update_job(job_id, status="verifying", progress_pct=100.0)
    ok, msg = transcode.verify_output(probed, dst)
    if not ok:
        log.error("[job %d] verify failed: %s", job_id, msg)
        update_job(job_id, status="failed",
                   error_msg=f"verify: {msg}",
                   finished_at=time.time())
        try:
            dst.unlink(missing_ok=True)
        except OSError:
            pass
        return

    dst_size = dst.stat().st_size
    update_job(job_id, dst_size=dst_size)

    if HOLD_FOR_APPROVAL:
        log.info("[job %d] holding for approval: %s -> %s",
                 job_id, _human(probed.path.stat().st_size), _human(dst_size))
        update_job(job_id, status="pending_approval", finished_at=time.time())
    else:
        _do_replace(job_id)


def _do_replace(job_id: int) -> None:
    """Atomic source replacement. Called either by worker (if not holding)
    or by user via /api/approve."""
    row = get_job(job_id)
    if not row:
        return
    src = Path(row["src_path"])
    dst = Path(row["dst_path"])
    if not dst.exists():
        update_job(job_id, status="failed",
                   error_msg="temp file vanished before approval")
        return

    final = src.with_suffix(".mkv")
    backup_path: Path | None = None

    try:
        if KEEP_ORIGINAL_BACKUP and src.exists():
            stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
            backup_path = src.with_name(src.name + f".original-{stamp}")
            os.rename(src, backup_path)
            log.info("[job %d] backed up source to %s", job_id, backup_path)
        elif src.exists():
            # No backup wanted: rename source out of the way until final mv succeeds.
            backup_path = src.with_name(src.name + ".to-delete")
            os.rename(src, backup_path)

        os.rename(dst, final)
        # Preserve mtime from source so Sonarr/Radarr don't re-import
        if backup_path and backup_path.exists():
            stat = backup_path.stat()
            os.utime(final, (stat.st_atime, stat.st_mtime))

        if not KEEP_ORIGINAL_BACKUP and backup_path:
            backup_path.unlink(missing_ok=True)
            backup_path = None

        update_job(
            job_id,
            status="done",
            backup_path=str(backup_path) if backup_path else None,
            approved_at=time.time(),
            dst_path=str(final),
        )
        log.info("[job %d] done. saved %s",
                 job_id, _human(row["src_size"] - (row["dst_size"] or 0)))
    except OSError as e:
        log.exception("[job %d] replace failed: %s", job_id, e)
        update_job(job_id, status="failed", error_msg=f"replace: {e}")


def _human(n: int | None) -> str:
    if n is None:
        return "?"
    n = float(n)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if abs(n) < 1024:
            return f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} PiB"


# -------------------- inotify watcher (Phase 2) --------------------

_watcher_thread: threading.Thread | None = None


def _watcher_loop() -> None:
    try:
        from inotify_simple import INotify, flags
    except ImportError:
        log.error("inotify_simple not installed, watcher disabled")
        return
    inotify = INotify()
    watch_flags = flags.CLOSE_WRITE | flags.MOVED_TO | flags.CREATE
    wds: dict[int, Path] = {}

    def _add_recursive(root: Path) -> None:
        for d in [root] + [p for p in root.rglob("*") if p.is_dir()]:
            try:
                wd = inotify.add_watch(str(d), watch_flags)
                wds[wd] = d
            except OSError as e:
                log.warning("watch failed for %s: %s", d, e)

    for r in MEDIA_ROOTS:
        if r.exists():
            log.info("watcher: adding %s", r)
            _add_recursive(r)

    log.info("watcher running (%d directories)", len(wds))
    pending: dict[Path, float] = {}  # path -> earliest-process-time

    while True:
        events = inotify.read(timeout=2000)
        now = time.time()
        for ev in events:
            d = wds.get(ev.wd)
            if not d:
                continue
            p = d / ev.name
            if p.is_dir() and (ev.mask & flags.CREATE):
                _add_recursive(p)
                continue
            if p.suffix.lower() in transcode.VIDEO_EXTS:
                pending[p] = now + 30  # 30s settle
        # Process settled files
        for p, t in list(pending.items()):
            if t <= now:
                pending.pop(p, None)
                if not p.exists():
                    continue
                try:
                    enqueue_path(p, reason="watcher")
                except Exception as e:
                    log.warning("watcher enqueue failed for %s: %s", p, e)


# -------------------- FastAPI app --------------------

app = FastAPI(title="auto-transcode")
templates = Jinja2Templates(directory="/app/templates")
app.mount("/static", StaticFiles(directory="/app/static"), name="static")


@app.on_event("startup")
def _startup() -> None:
    db()  # init schema
    # Reset any "running"/"verifying" jobs left from a previous crash
    with db_lock() as c:
        c.execute("""
            UPDATE jobs SET status='failed',
                            error_msg='service restarted mid-job',
                            finished_at=?
            WHERE status IN ('running','verifying')
        """, (time.time(),))
        # Re-queue anything still queued
        rows = c.execute("SELECT id FROM jobs WHERE status='queued'").fetchall()
    for row in rows:
        _worker_queue.put(row["id"])

    global _worker_thread, _watcher_thread
    _worker_thread = threading.Thread(target=_worker_loop, daemon=True, name="worker")
    _worker_thread.start()

    if WATCHER_ENABLED:
        _watcher_thread = threading.Thread(target=_watcher_loop, daemon=True, name="watcher")
        _watcher_thread.start()
    else:
        log.info("watcher disabled (set WATCHER_ENABLED=true to turn on)")

    log.info("config: allowlist=%r watcher=%s hold=%s keep_backup=%s preset=%s crf=%s",
             ALLOWLIST_GLOB, WATCHER_ENABLED, HOLD_FOR_APPROVAL,
             KEEP_ORIGINAL_BACKUP, X265_PRESET, X265_CRF)


@app.get("/", response_class=HTMLResponse)
def index(request: Request):
    return templates.TemplateResponse("index.html", {
        "request": request,
        "allowlist": ALLOWLIST_GLOB,
        "watcher_enabled": WATCHER_ENABLED,
        "hold_for_approval": HOLD_FOR_APPROVAL,
    })


@app.get("/api/state")
def api_state():
    active = None
    if _active_job_id:
        row = get_job(_active_job_id)
        if row:
            active = _row_to_dict(row)
    queued = [_row_to_dict(r) for r in jobs_by_status("queued")]
    pending = [_row_to_dict(r) for r in jobs_by_status("pending_approval")]
    done = [_row_to_dict(r) for r in jobs_by_status("done")][:50]
    failed = [_row_to_dict(r) for r in jobs_by_status("failed", "discarded")][:25]
    return {
        "active": active,
        "queued": queued,
        "pending": pending,
        "done": done,
        "failed": failed,
        "total_saved_bytes": total_savings_bytes(),
        "config": {
            "allowlist": ALLOWLIST_GLOB,
            "watcher_enabled": WATCHER_ENABLED,
            "hold_for_approval": HOLD_FOR_APPROVAL,
            "keep_original_backup": KEEP_ORIGINAL_BACKUP,
            "preset": X265_PRESET,
            "crf": X265_CRF,
        },
    }


@app.post("/api/scan")
def api_scan():
    result = scan_and_enqueue()
    return result


@app.post("/api/enqueue")
def api_enqueue(payload: dict):
    raw = (payload or {}).get("path", "").strip()
    if not raw:
        raise HTTPException(400, "path required")
    p = Path(raw)
    try:
        jid = enqueue_path(p, reason="manual")
    except FileNotFoundError:
        raise HTTPException(404, f"not found: {p}")
    if jid is None:
        raise HTTPException(409, "rejected (allowlist or duplicate)")
    return {"job_id": jid}


@app.post("/api/approve/{job_id}")
def api_approve(job_id: int):
    row = get_job(job_id)
    if not row:
        raise HTTPException(404, "no such job")
    if row["status"] != "pending_approval":
        raise HTTPException(409, f"job status is {row['status']}")
    _do_replace(job_id)
    return {"ok": True}


@app.post("/api/discard/{job_id}")
def api_discard(job_id: int):
    row = get_job(job_id)
    if not row:
        raise HTTPException(404, "no such job")
    if row["status"] != "pending_approval":
        raise HTTPException(409, f"job status is {row['status']}")
    if row["dst_path"]:
        try:
            Path(row["dst_path"]).unlink(missing_ok=True)
        except OSError as e:
            log.warning("discard: failed to remove %s: %s", row["dst_path"], e)
    update_job(job_id, status="discarded")
    return {"ok": True}


def _row_to_dict(row: sqlite3.Row) -> dict:
    d = dict(row)
    # Friendly fields
    d["src_size_human"] = _human(d.get("src_size"))
    d["dst_size_human"] = _human(d.get("dst_size")) if d.get("dst_size") else None
    saved = None
    if d.get("dst_size") and d.get("src_size"):
        saved = d["src_size"] - d["dst_size"]
    d["saved_bytes"] = saved
    d["saved_human"] = _human(saved) if saved is not None else None
    if saved is not None and d.get("src_size"):
        d["saved_pct"] = round(100.0 * saved / d["src_size"], 1)
    return d
