"""
sufi2.api
=========
FastAPI application exposing SUFI-2 calibration as HTTP endpoints.

Endpoints
---------
  GET  /              — serves the Web UI (index.html)
  POST /run           — start a calibration job (returns job_id)
  GET  /status/{id}   — job status + streamed log tail
  GET  /logs/{id}     — Server-Sent Events live log stream
  GET  /results/{id}  — download results as a zip archive
  GET  /jobs          — list all jobs

Usage
-----
  uvicorn sufi2.api:app --host 0.0.0.0 --port 8000
"""

from __future__ import annotations

import asyncio
import io
import json
import logging
import os
import shutil
import tempfile
import threading
import time
import uuid
import zipfile
from pathlib import Path
from typing import Any, Dict, Optional

import yaml
from fastapi import BackgroundTasks, FastAPI, HTTPException, Request, UploadFile, File, Form
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from sufi2.config import SUFI2Config

log = logging.getLogger("sufi2.api")

# ─────────────────────────────────────────────────────────────────────────────
# In-memory job store
# ─────────────────────────────────────────────────────────────────────────────

class Job:
    def __init__(self, job_id: str, work_dir: Path):
        self.job_id   = job_id
        self.work_dir = work_dir
        self.status   = "queued"   # queued | running | done | error
        self.progress = 0.0
        self.logs: list[str] = []
        self.result: Optional[dict] = None
        self.error:  Optional[str]  = None
        self.created_at = time.time()
        self._lock = threading.Lock()

    def add_log(self, msg: str):
        with self._lock:
            self.logs.append(f"[{time.strftime('%H:%M:%S')}] {msg}")

    def to_dict(self) -> dict:
        with self._lock:
            return {
                "job_id":     self.job_id,
                "status":     self.status,
                "progress":   round(self.progress * 100, 1),
                "log_lines":  len(self.logs),
                "result":     self.result,
                "error":      self.error,
                "created_at": self.created_at,
            }


JOBS: Dict[str, Job] = {}
JOBS_LOCK = threading.Lock()

# ─────────────────────────────────────────────────────────────────────────────
# App
# ─────────────────────────────────────────────────────────────────────────────

app = FastAPI(
    title="SUFI-2 SWAT Calibration API",
    version="1.0.0",
    description="REST API for running SUFI-2 multi-reach SWAT calibrations.",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.middleware("http")
async def add_security_headers(request, call_next):
    response = await call_next(request)
    # Prevent iframe embedding from foreign origins
    response.headers["X-Frame-Options"] = "SAMEORIGIN"
    # Prevent MIME sniffing
    response.headers["X-Content-Type-Options"] = "nosniff"
    return response

# Serve static UI
_UI_DIR = Path(__file__).parent / "ui"
if _UI_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(_UI_DIR)), name="static")


# ─────────────────────────────────────────────────────────────────────────────
# Routes
# ─────────────────────────────────────────────────────────────────────────────

@app.api_route("/", methods=["GET", "HEAD"], response_class=HTMLResponse, include_in_schema=False)
async def serve_ui(request: Request):
    """Serve the Web UI. HEAD is accepted for Render health checks."""
    ui_file = _UI_DIR / "index.html"
    body = ui_file.read_text(encoding="utf-8") if ui_file.exists() else            "<h1>SUFI-2 API</h1><p>UI not found. See <a href='/docs'>/docs</a>.</p>"
    if request.method == "HEAD":
        return Response(status_code=200, headers={"content-type": "text/html"})
    return HTMLResponse(content=body)


@app.api_route("/health", methods=["GET", "HEAD"])
async def health(request: Request):
    if request.method == "HEAD":
        return Response(status_code=200)
    return {"status": "ok"}


@app.get("/scan")
async def scan_work_dir(path: str = "."):
    """
    Scan a directory and report which SWAT files were auto-detected.
    Used by the UI to validate the work_dir before submitting a run.

    Returns a structured inventory so the UI can show a green/amber/red
    status for each expected file type.
    """
    work_dir = Path(path).expanduser().resolve()

    result = {
        "path":      str(work_dir),
        "exists":    work_dir.exists(),
        "files":     {},
        "ready":     False,
        "warnings":  [],
        "errors":    [],
    }

    if not work_dir.exists():
        result["errors"].append(f"Directory not found: {work_dir}")
        return result

    # ── file.cio ──────────────────────────────────────────────────────────
    cio = next(iter(sorted(work_dir.glob("file.cio")) + sorted(work_dir.glob("*.cio"))), None)
    result["files"]["file_cio"] = {"found": bool(cio), "name": cio.name if cio else None}

    # ── output.rch ────────────────────────────────────────────────────────
    rch = next(iter(sorted(work_dir.glob("output.rch")) + sorted(work_dir.glob("*.rch"))), None)
    result["files"]["output_rch"] = {"found": bool(rch), "name": rch.name if rch else None,
                                      "size_kb": round(rch.stat().st_size / 1024, 1) if rch else None}

    # ── observed flow CSVs ────────────────────────────────────────────────
    obs = sorted(work_dir.glob("observed_flow_rch*.csv")) or sorted(work_dir.glob("obs_flow_rch*.csv"))
    result["files"]["observed_flow"] = {
        "found": bool(obs),
        "count": len(obs),
        "names": [f.name for f in obs],
    }

    # ── SWAT input files ──────────────────────────────────────────────────
    for ext in [".mgt", ".gw", ".sol", ".hru", ".rte", ".bsn"]:
        found = sorted(work_dir.glob(f"*{ext}"))
        result["files"][ext.lstrip(".")] = {"found": bool(found), "count": len(found)}

    # ── par_inf ───────────────────────────────────────────────────────────
    par = next(
        iter(sorted(work_dir.glob("par_inf.txt")) + sorted(work_dir.glob("Par_inf.txt"))
             + sorted(work_dir.glob("param_ranges.txt"))),
        None,
    )
    result["files"]["par_inf"] = {
        "found": bool(par),
        "name":  par.name if par else None,
        "note":  "built-in defaults will be used" if not par else None,
    }

    # ── SWAT exe ──────────────────────────────────────────────────────────
    import shutil as _shutil
    exe_candidates = ["swat2012.exe", "swat_rel.exe", "swat.exe", "swat2012", "swat_rel", "swat"]
    exe = None
    for c in exe_candidates:
        p = work_dir / c
        if p.exists():
            exe = p
            break
        w = _shutil.which(c)
        if w:
            exe = Path(w)
            break
    result["files"]["swat_exe"] = {
        "found": bool(exe),
        "name":  exe.name if exe else None,
        "note":  "mock runner will be used" if not exe else None,
    }

    # ── Readiness assessment ──────────────────────────────────────────────
    if not result["files"]["output_rch"]["found"]:
        result["errors"].append("output.rch not found — run SWAT once to generate it")
    if not result["files"]["observed_flow"]["found"]:
        result["errors"].append("No observed_flow_rchN.csv files found")
    if not result["files"]["file_cio"]["found"]:
        result["warnings"].append("file.cio not found (optional — dates will be inferred)")
    if not result["files"]["swat_exe"]["found"]:
        result["warnings"].append("SWAT executable not found — mock runner will be used")
    if not result["files"]["par_inf"]["found"]:
        result["warnings"].append("par_inf.txt not found — built-in default parameters will be used")

    result["ready"] = len(result["errors"]) == 0
    return result


@app.get("/parameters")
async def get_parameters():
    """
    Return the full built-in parameter catalogue with default ranges.
    The UI uses this to populate the parameter selection table.
    """
    from sufi2.core import DEFAULT_PARAMS
    return {
        "parameters": DEFAULT_PARAMS.to_dict(orient="records"),
        "change_types": {
            "v": "Replace — set parameter directly to new value",
            "r": "Relative — original × (1 + new_val)",
            "a": "Additive — original + new_val",
        },
    }


@app.get("/jobs")
async def list_jobs():
    """List all jobs."""
    with JOBS_LOCK:
        return [j.to_dict() for j in sorted(JOBS.values(), key=lambda j: j.created_at, reverse=True)]


@app.post("/run", status_code=202)
async def start_run(
    background_tasks: BackgroundTasks,
    config_file: Optional[UploadFile] = File(None, description="config.yaml"),
    config_json: Optional[str]        = Form(None, description="Config as JSON string (alternative to file)"),
    data_files:  list[UploadFile]     = File(default=[], description="SWAT input files (output.rch, observed_flow_rchN.csv, *.mgt, etc.)"),
):
    """
    Start a calibration job.

    Supply either:
    - `config_file`  — a config.yaml upload
    - `config_json`  — config as a JSON string (from the Web UI form)

    Optionally upload SWAT data files. If omitted, the tool reads from `paths.work_dir`.
    """
    # Parse config
    try:
        if config_file is not None:
            raw  = await config_file.read()
            data = yaml.safe_load(raw.decode())
        elif config_json:
            data = json.loads(config_json)
        else:
            raise ValueError("Provide config_file or config_json")
    except Exception as e:
        raise HTTPException(status_code=422, detail=f"Config parse error: {e}")

    # Extract inline selected params (from UI parameter table) before building config
    selected_params = data.pop("_selected_params", None)

    try:
        cfg = SUFI2Config.from_dict(data)
    except Exception as e:
        raise HTTPException(status_code=422, detail=f"Config validation error: {e}")

    # Set up job ID and a scratch dir for generated files (par_inf, etc.)
    job_id   = str(uuid.uuid4())[:8]
    scratch  = Path(tempfile.mkdtemp(prefix=f"sufi2_{job_id}_"))

    # Determine the effective work_dir:
    # - If data files are uploaded → extract them to scratch and use scratch
    # - Otherwise → use the work_dir the user typed in config (must exist on server)
    if data_files:
        for uf in data_files:
            dest = scratch / uf.filename
            raw  = await uf.read()
            dest.write_bytes(raw)
        effective_work_dir = scratch
        cfg = cfg.model_copy(update={"paths": cfg.paths.model_copy(update={"work_dir": str(scratch)})})
    else:
        effective_work_dir = cfg.paths.work_path
        # Validate it exists before queuing
        if not effective_work_dir.exists():
            raise HTTPException(
                status_code=422,
                detail=f"work_dir does not exist on the server: {effective_work_dir}. "
                       "Either upload your data files, or set work_dir to a path that exists inside the container."
            )

    # Write par_inf.txt into the effective work_dir so the engine finds it
    if selected_params:
        par_lines = ["# par_inf.txt — generated by SUFI-2 UI"]
        for p in selected_params:
            par_lines.append(f"{p['name']:<20} {p['min']:>12} {p['max']:>12}  {p['change_type']}")
        par_path = effective_work_dir / "par_inf.txt"
        par_path.write_text("\n".join(par_lines) + "\n")
        cfg = cfg.model_copy(update={"paths": cfg.paths.model_copy(update={"par_inf": str(par_path)})})

    job = Job(job_id=job_id, work_dir=effective_work_dir)
    with JOBS_LOCK:
        JOBS[job_id] = job

    background_tasks.add_task(_run_job, job, cfg)
    return {"job_id": job_id, "status": "queued", "message": "Calibration job queued."}


@app.get("/status/{job_id}")
async def job_status(job_id: str):
    """Get job status, progress, and last 50 log lines."""
    job = _get_job(job_id)
    d = job.to_dict()
    with job._lock:
        d["recent_logs"] = job.logs[-50:]
    return d


@app.get("/logs/{job_id}")
async def stream_logs(job_id: str):
    """
    Server-Sent Events stream of live log output.

    Connect with:
        const es = new EventSource('/logs/<job_id>');
        es.onmessage = e => console.log(e.data);
    """
    job = _get_job(job_id)

    async def generate():
        sent = 0
        while True:
            with job._lock:
                new_lines = job.logs[sent:]
                current_status = job.status
            for line in new_lines:
                yield f"data: {line}\n\n"
            sent += len(new_lines)
            if current_status in ("done", "error") and not new_lines:
                yield f"data: [STREAM END — job {current_status}]\n\n"
                break
            await asyncio.sleep(0.5)

    return StreamingResponse(generate(), media_type="text/event-stream")


@app.get("/results/{job_id}")
async def download_results(job_id: str):
    """Download all results as a zip archive."""
    job = _get_job(job_id)
    if job.status != "done":
        raise HTTPException(status_code=409, detail=f"Job is {job.status}, not done yet.")

    results_dir = job.work_dir / "sufi2_results"
    if not results_dir.exists():
        raise HTTPException(status_code=404, detail="Results directory not found.")

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for f in results_dir.rglob("*"):
            if f.is_file():
                zf.write(f, f.relative_to(job.work_dir))
    buf.seek(0)
    return StreamingResponse(
        buf,
        media_type="application/zip",
        headers={"Content-Disposition": f"attachment; filename=sufi2_results_{job_id}.zip"},
    )


@app.delete("/jobs/{job_id}", status_code=204)
async def delete_job(job_id: str):
    """Remove a completed job and its temp files."""
    job = _get_job(job_id)
    if job.status == "running":
        raise HTTPException(status_code=409, detail="Cannot delete a running job.")
    # Only remove auto-created scratch dirs (prefix sufi2_), never user's real work_dir
    wd = str(job.work_dir)
    if "sufi2_" in Path(wd).name and Path(wd).parent == Path(tempfile.gettempdir()):
        shutil.rmtree(job.work_dir, ignore_errors=True)
    with JOBS_LOCK:
        JOBS.pop(job_id, None)


# ─────────────────────────────────────────────────────────────────────────────
# Background runner
# ─────────────────────────────────────────────────────────────────────────────

def _run_job(job: Job, cfg: SUFI2Config):
    from sufi2.core import SUFI2Engine

    job.status = "running"
    job.add_log("Job started.")

    def progress(msg: str, pct: float):
        job.progress = pct
        job.add_log(msg)

    try:
        engine = SUFI2Engine(cfg, progress_callback=progress)
        result = engine.run()
        job.result = result
        job.status = "done"
        job.progress = 1.0
        job.add_log("Job completed successfully.")
    except Exception as e:
        job.error  = str(e)
        job.status = "error"
        job.add_log(f"ERROR: {e}")
        log.exception("Job %s failed", job.job_id)
    finally:
        # Clean up temp work dir if results were saved elsewhere
        pass


def _get_job(job_id: str) -> Job:
    with JOBS_LOCK:
        job = JOBS.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail=f"Job '{job_id}' not found.")
    return job
