"""
sufi2.db — Neon (PostgreSQL) persistence layer for Hydro-Modeller.

Stores:
  - jobs          : calibration job history with status, config, metrics
  - projects      : saved user projects (name, config, selected params)
  - results       : per-job performance metrics (NSE, KGE, PBIAS per reach)

Falls back gracefully to in-memory mode if DATABASE_URL is not set.
"""
from __future__ import annotations

import json
import logging
import os
import time
from typing import Optional

log = logging.getLogger("sufi2.db")

DATABASE_URL = os.getenv("DATABASE_URL") or os.getenv("NEON_DATABASE_URL")

_conn = None
_db_available = False


def _get_conn():
    """Return a live psycopg2 connection, reconnecting if needed."""
    global _conn, _db_available
    if not DATABASE_URL:
        return None
    try:
        import psycopg2
        import psycopg2.extras
        if _conn is None or _conn.closed:
            _conn = psycopg2.connect(DATABASE_URL, connect_timeout=5)
            _conn.autocommit = True
            _db_available = True
        return _conn
    except Exception as e:
        log.warning(f"DB connection failed: {e}")
        _db_available = False
        return None


def init_db():
    """Create tables if they don't exist. Safe to call multiple times."""
    conn = _get_conn()
    if not conn:
        log.info("No DATABASE_URL — running in memory-only mode")
        return False
    try:
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS jobs (
                    job_id      TEXT PRIMARY KEY,
                    status      TEXT NOT NULL DEFAULT 'queued',
                    phase       TEXT NOT NULL DEFAULT 'calibration',
                    created_at  DOUBLE PRECISION NOT NULL,
                    finished_at DOUBLE PRECISION,
                    config_json JSONB,
                    result_json JSONB,
                    error_msg   TEXT,
                    log_lines   INTEGER DEFAULT 0,
                    best_nse    REAL,
                    best_kge    REAL
                );

                CREATE TABLE IF NOT EXISTS projects (
                    id          SERIAL PRIMARY KEY,
                    name        TEXT NOT NULL,
                    config_json JSONB NOT NULL,
                    created_at  TIMESTAMP DEFAULT NOW(),
                    updated_at  TIMESTAMP DEFAULT NOW(),
                    UNIQUE(name)
                );

                CREATE TABLE IF NOT EXISTS results (
                    id          SERIAL PRIMARY KEY,
                    job_id      TEXT REFERENCES jobs(job_id) ON DELETE CASCADE,
                    reach_id    INTEGER NOT NULL,
                    phase       TEXT NOT NULL DEFAULT 'calibration',
                    nse         REAL,
                    kge         REAL,
                    pbias       REAL,
                    r2          REAL,
                    created_at  TIMESTAMP DEFAULT NOW()
                );

                CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status);
                CREATE INDEX IF NOT EXISTS idx_jobs_created ON jobs(created_at DESC);
                CREATE INDEX IF NOT EXISTS idx_results_job ON results(job_id);
            """)
        log.info("Database tables initialised ✓")
        return True
    except Exception as e:
        log.error(f"DB init failed: {e}")
        return False


# ── Job persistence ───────────────────────────────────────────────────────────

def save_job(job_id: str, status: str, phase: str, created_at: float,
             config: Optional[dict] = None, result: Optional[dict] = None,
             error: Optional[str] = None, log_lines: int = 0):
    conn = _get_conn()
    if not conn:
        return
    try:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO jobs (job_id, status, phase, created_at, config_json,
                                  result_json, error_msg, log_lines)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (job_id) DO UPDATE SET
                    status      = EXCLUDED.status,
                    phase       = EXCLUDED.phase,
                    result_json = EXCLUDED.result_json,
                    error_msg   = EXCLUDED.error_msg,
                    log_lines   = EXCLUDED.log_lines,
                    finished_at = CASE WHEN EXCLUDED.status IN ('done','error','cancelled')
                                  THEN EXTRACT(EPOCH FROM NOW()) ELSE jobs.finished_at END
            """, (
                job_id, status, phase, created_at,
                json.dumps(config) if config else None,
                json.dumps(result) if result else None,
                error, log_lines
            ))
    except Exception as e:
        log.warning(f"save_job failed: {e}")


def save_results(job_id: str, phase: str, reach_metrics: dict):
    """Save per-reach performance metrics."""
    conn = _get_conn()
    if not conn or not reach_metrics:
        return
    try:
        with conn.cursor() as cur:
            # Delete existing results for this job+phase
            cur.execute("DELETE FROM results WHERE job_id=%s AND phase=%s", (job_id, phase))
            for rid, m in reach_metrics.items():
                cur.execute("""
                    INSERT INTO results (job_id, reach_id, phase, nse, kge, pbias, r2)
                    VALUES (%s, %s, %s, %s, %s, %s, %s)
                """, (job_id, int(rid), phase,
                      m.get("NSE"), m.get("KGE"), m.get("PBIAS"), m.get("R2")))
            # Update best NSE on jobs table
            best = max((m.get("NSE") or -999 for m in reach_metrics.values()), default=None)
            if best and best > -999:
                cur.execute("UPDATE jobs SET best_nse=%s WHERE job_id=%s", (best, job_id))
    except Exception as e:
        log.warning(f"save_results failed: {e}")


def list_jobs_from_db(limit: int = 50) -> list:
    conn = _get_conn()
    if not conn:
        return []
    try:
        import psycopg2.extras
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("""
                SELECT job_id, status, phase, created_at, finished_at,
                       error_msg, log_lines, best_nse, best_kge
                FROM jobs ORDER BY created_at DESC LIMIT %s
            """, (limit,))
            return [dict(r) for r in cur.fetchall()]
    except Exception as e:
        log.warning(f"list_jobs_from_db failed: {e}")
        return []


# ── Project persistence ───────────────────────────────────────────────────────

def save_project_db(name: str, config: dict) -> bool:
    conn = _get_conn()
    if not conn:
        return False
    try:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO projects (name, config_json, updated_at)
                VALUES (%s, %s, NOW())
                ON CONFLICT (name) DO UPDATE SET
                    config_json = EXCLUDED.config_json,
                    updated_at  = NOW()
            """, (name, json.dumps(config)))
        return True
    except Exception as e:
        log.warning(f"save_project_db failed: {e}")
        return False


def load_project_db(name: str) -> Optional[dict]:
    conn = _get_conn()
    if not conn:
        return None
    try:
        import psycopg2.extras
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT config_json FROM projects WHERE name=%s", (name,))
            row = cur.fetchone()
            return row["config_json"] if row else None
    except Exception as e:
        log.warning(f"load_project_db failed: {e}")
        return None


def list_projects_db() -> list:
    conn = _get_conn()
    if not conn:
        return []
    try:
        import psycopg2.extras
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT name, updated_at FROM projects ORDER BY updated_at DESC")
            return [dict(r) for r in cur.fetchall()]
    except Exception as e:
        log.warning(f"list_projects_db failed: {e}")
        return []


def delete_project_db(name: str) -> bool:
    conn = _get_conn()
    if not conn:
        return False
    try:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM projects WHERE name=%s", (name,))
        return True
    except Exception as e:
        log.warning(f"delete_project_db failed: {e}")
        return False


def db_status() -> dict:
    """Return database connection status for health check."""
    conn = _get_conn()
    if not conn:
        return {"connected": False, "mode": "memory-only", "url_set": bool(DATABASE_URL)}
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM jobs")
            job_count = cur.fetchone()[0]
            cur.execute("SELECT COUNT(*) FROM projects")
            proj_count = cur.fetchone()[0]
        return {
            "connected": True,
            "mode": "postgresql",
            "jobs": job_count,
            "projects": proj_count,
        }
    except Exception as e:
        return {"connected": False, "error": str(e)}
