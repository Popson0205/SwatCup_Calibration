"""
sufi2.api — FastAPI application for SUFI-2 SWAT calibration.

Phased endpoints:
  POST /jobs/calibrate          — start calibration job
  POST /jobs/{id}/validate      — run validation with best params
  POST /jobs/{id}/hindcast      — run hindcast
  POST /jobs/{id}/forecast      — run forecast
  GET  /jobs/{id}/status        — job status + logs
  GET  /jobs/{id}/logs          — SSE live log stream
  GET  /jobs/{id}/results       — download results ZIP
  GET  /jobs/{id}/files         — list result files
  GET  /jobs                    — list all jobs
  GET  /parameters              — built-in parameter catalogue
  GET  /scan                    — scan server-side directory
  GET  /health                  — health check
  GET  /                        — Web UI
"""
from __future__ import annotations

import asyncio, gc, io, json, logging, math, shutil, tempfile, threading, time, uuid, zipfile
from pathlib import Path
from typing import Dict, List, Optional

import yaml
from fastapi import BackgroundTasks, FastAPI, HTTPException, Request, UploadFile, File, Form
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles

from sufi2.config import SUFI2Config
from sufi2.logger import get_logger, read_log_tail, get_log_path

log = get_logger("sufi2.api")


def _sanitise(obj):
    """Recursively replace inf/nan with None so JSON serialisation never fails."""
    if isinstance(obj, float):
        return None if (math.isnan(obj) or math.isinf(obj)) else obj
    if isinstance(obj, dict):
        return {k: _sanitise(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_sanitise(v) for v in obj]
    return obj

# ─────────────────────────────────────────────────────────────────────────────
# Job store
# ─────────────────────────────────────────────────────────────────────────────

class Job:
    def __init__(self, job_id: str, work_dir: Path, scratch: Optional[Path] = None):
        self.job_id    = job_id
        self.work_dir  = work_dir   # effective SWAT files dir
        self.scratch   = scratch    # temp dir to clean up (if files were uploaded)
        self.status    = "queued"   # queued|running|done|error
        self.phase     = "calibration"
        self.progress  = 0.0
        self.logs: List[str] = []
        self.result: Optional[dict] = None
        self.error:  Optional[str]  = None
        self.created_at = time.time()
        self._lock = threading.Lock()
        self._engine = None   # SUFI2Engine instance (reused across phases)

    def emit(self, msg: str, pct: float = 0.0):
        with self._lock:
            self.progress = pct
            self.logs.append(f"[{time.strftime('%H:%M:%S')}] {msg}")

    def to_dict(self) -> dict:
        with self._lock:
            return {
                "job_id":    self.job_id,
                "status":    self.status,
                "phase":     self.phase,
                "progress":  round(self.progress * 100, 1),
                "log_lines": len(self.logs),
                "result":    self.result,
                "error":     self.error,
                "created_at": self.created_at,
            }


JOBS: Dict[str, Job] = {}
JOBS_LOCK = threading.Lock()

# ─────────────────────────────────────────────────────────────────────────────
# App
# ─────────────────────────────────────────────────────────────────────────────

app = FastAPI(title="SUFI-2 SWAT Calibration API", version="2.0.0")

app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

@app.middleware("http")
async def security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers["X-Frame-Options"]        = "SAMEORIGIN"
    response.headers["X-Content-Type-Options"] = "nosniff"
    return response

_UI_DIR = Path(__file__).parent / "ui"
if _UI_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(_UI_DIR)), name="static")

# ─────────────────────────────────────────────────────────────────────────────
# Core routes
# ─────────────────────────────────────────────────────────────────────────────

@app.api_route("/", methods=["GET", "HEAD"], response_class=HTMLResponse, include_in_schema=False)
async def serve_ui(request: Request):
    ui_file = _UI_DIR / "index.html"
    body = ui_file.read_text(encoding="utf-8") if ui_file.exists() else \
           "<h1>SUFI-2 API</h1><p>UI not found. See <a href='/docs'>/docs</a>.</p>"
    if request.method == "HEAD":
        return Response(status_code=200, headers={"content-type": "text/html"})
    return HTMLResponse(content=body)

@app.api_route("/health", methods=["GET", "HEAD"])
async def health(request: Request):
    if request.method == "HEAD":
        return Response(status_code=200)
    return {"status": "ok"}

@app.get("/parameters")
async def get_parameters():
    from sufi2.core import DEFAULT_PARAMS
    return {
        "parameters": DEFAULT_PARAMS.to_dict(orient="records"),
        "change_types": {
            "v": "Replace — set parameter directly to new value",
            "r": "Relative — original × (1 + new_val)",
            "a": "Additive — original + new_val",
        },
    }

@app.get("/scan")
async def scan_work_dir(path: str = "."):
    work_dir = Path(path).expanduser().resolve()
    result = {"path": str(work_dir), "exists": work_dir.exists(),
              "files": {}, "ready": False, "warnings": [], "errors": []}
    if not work_dir.exists():
        result["errors"].append(f"Directory not found: {work_dir}")
        return result

    cio = next(iter(sorted(work_dir.glob("file.cio")) + sorted(work_dir.glob("*.cio"))), None)
    result["files"]["file_cio"] = {"found": bool(cio), "name": cio.name if cio else None}

    rch = next(iter(sorted(work_dir.glob("output.rch")) + sorted(work_dir.glob("*.rch"))), None)
    result["files"]["output_rch"] = {"found": bool(rch), "name": rch.name if rch else None,
                                      "size_kb": round(rch.stat().st_size/1024,1) if rch else None}

    obs = sorted(work_dir.glob("observed_flow_rch*.csv")) or sorted(work_dir.glob("obs_flow_rch*.csv"))
    result["files"]["observed_flow"] = {"found": bool(obs), "count": len(obs), "names": [f.name for f in obs]}

    for ext in [".mgt",".gw",".sol",".hru",".rte",".bsn"]:
        found = sorted(work_dir.glob(f"*{ext}"))
        result["files"][ext.lstrip(".")] = {"found": bool(found), "count": len(found)}

    par = next(iter(sorted(work_dir.glob("par_inf.txt")) + sorted(work_dir.glob("Par_inf.txt"))
                    + sorted(work_dir.glob("param_ranges.txt"))), None)
    result["files"]["par_inf"] = {"found": bool(par), "name": par.name if par else None,
                                   "note": "built-in defaults will be used" if not par else None}

    exe_candidates = ["swat2012.exe","swat_rel.exe","swat.exe","swat2012","swat_rel","swat"]
    exe = None
    for c in exe_candidates:
        p = work_dir / c
        if p.exists(): exe = p; break
        w = shutil.which(c)
        if w: exe = Path(w); break
    result["files"]["swat_exe"] = {"found": bool(exe), "name": exe.name if exe else None,
                                    "note": "mock runner will be used" if not exe else None}

    if not result["files"]["output_rch"]["found"]:
        result["errors"].append("output.rch not found — run SWAT once first")
    if not result["files"]["observed_flow"]["found"]:
        result["errors"].append("No observed_flow_rchN.csv files found")
    if not result["files"]["file_cio"]["found"]:
        result["warnings"].append("file.cio not found (optional)")
    if not result["files"]["swat_exe"]["found"]:
        result["warnings"].append("SWAT exe not found — mock runner will be used")
    if not result["files"]["par_inf"]["found"]:
        result["warnings"].append("par_inf.txt not found — built-in defaults used")

    result["ready"] = len(result["errors"]) == 0
    return result


@app.get("/logs/file")
async def get_log_file(lines: int = 200):
    """
    Return the last N lines of the application log file.
    Useful for debugging on Render — visit /logs/file in your browser.
    """
    return {
        "log_path": get_log_path(),
        "lines":    lines,
        "content":  read_log_tail(lines),
    }

# ─────────────────────────────────────────────────────────────────────────────
# Job management
# ─────────────────────────────────────────────────────────────────────────────

@app.get("/jobs")
async def list_jobs():
    with JOBS_LOCK:
        return [j.to_dict() for j in sorted(JOBS.values(), key=lambda j: j.created_at, reverse=True)]

def _get_job(job_id: str) -> Job:
    with JOBS_LOCK:
        job = JOBS.get(job_id)
    if not job:
        raise HTTPException(404, f"Job '{job_id}' not found.")
    return job

async def _parse_request(config_file, config_json) -> dict:
    try:
        if config_file is not None:
            raw  = await config_file.read()
            data = yaml.safe_load(raw.decode())
        elif config_json:
            data = json.loads(config_json)
        else:
            raise ValueError("Provide config_file or config_json")
    except Exception as e:
        raise HTTPException(422, f"Config parse error: {e}")
    return data

async def _setup_job(config_file, config_json, data_files) -> tuple:
    """Parse config, handle file uploads, return (job_id, job, cfg)."""
    data = await _parse_request(config_file, config_json)
    selected_params = data.pop("_selected_params", None)
    try:
        cfg = SUFI2Config.from_dict(data)
    except Exception as e:
        raise HTTPException(422, f"Config validation error: {e}")

    job_id = str(uuid.uuid4())[:8]
    scratch = None

    if data_files:
        scratch = Path(tempfile.mkdtemp(prefix=f"sufi2_{job_id}_"))
        for uf in data_files:
            raw = await uf.read()
            (scratch / uf.filename).write_bytes(raw)
        effective_work_dir = scratch
        cfg = cfg.model_copy(update={"paths": cfg.paths.model_copy(update={"work_dir": str(scratch)})})
    else:
        effective_work_dir = cfg.paths.work_path
        if not effective_work_dir.exists():
            raise HTTPException(422, f"work_dir not found on server: {effective_work_dir}. Upload files instead.")

    if selected_params:
        par_lines = ["# par_inf.txt — generated by SUFI-2 UI"]
        for p in selected_params:
            par_lines.append(f"{p['name']:<20} {p['min']:>12} {p['max']:>12}  {p['change_type']}")
        par_path = effective_work_dir / "par_inf.txt"
        par_path.write_text("\n".join(par_lines) + "\n")
        cfg = cfg.model_copy(update={"paths": cfg.paths.model_copy(update={"par_inf": str(par_path)})})

    job = Job(job_id=job_id, work_dir=effective_work_dir, scratch=scratch)
    with JOBS_LOCK:
        JOBS[job_id] = job

    return job_id, job, cfg

# ─────────────────────────────────────────────────────────────────────────────
# Phase endpoints
# ─────────────────────────────────────────────────────────────────────────────

@app.post("/jobs/calibrate", status_code=202)
async def start_calibration(
    background_tasks: BackgroundTasks,
    config_file: Optional[UploadFile] = File(None),
    config_json: Optional[str]        = Form(None),
    data_files:  List[UploadFile]     = File(default=[]),
):
    """Start a calibration job. Returns job_id to use for subsequent phases."""
    job_id, job, cfg = await _setup_job(config_file, config_json, data_files)
    background_tasks.add_task(_run_phase, job, cfg, "calibration")
    return {"job_id": job_id, "phase": "calibration", "status": "queued"}

@app.post("/jobs/{job_id}/validate", status_code=202)
async def start_validation(job_id: str, background_tasks: BackgroundTasks):
    """Run validation using best params from calibration."""
    job = _get_job(job_id)
    if job.status == "running":
        raise HTTPException(409, "Job is already running.")
    if job._engine is None:
        raise HTTPException(409, "Calibration must complete before validation.")
    background_tasks.add_task(_run_phase_existing, job, "validation")
    return {"job_id": job_id, "phase": "validation", "status": "queued"}

@app.post("/jobs/{job_id}/hindcast", status_code=202)
async def start_hindcast(job_id: str, background_tasks: BackgroundTasks):
    """Run hindcast using best params from calibration."""
    job = _get_job(job_id)
    if job.status == "running":
        raise HTTPException(409, "Job is already running.")
    if job._engine is None:
        raise HTTPException(409, "Calibration must complete before hindcast.")
    background_tasks.add_task(_run_phase_existing, job, "hindcast")
    return {"job_id": job_id, "phase": "hindcast", "status": "queued"}

@app.post("/jobs/{job_id}/forecast", status_code=202)
async def start_forecast(job_id: str, background_tasks: BackgroundTasks):
    """Run forecast using best params from calibration."""
    job = _get_job(job_id)
    if job.status == "running":
        raise HTTPException(409, "Job is already running.")
    if job._engine is None:
        raise HTTPException(409, "Calibration must complete before forecast.")
    background_tasks.add_task(_run_phase_existing, job, "forecast")
    return {"job_id": job_id, "phase": "forecast", "status": "queued"}

@app.get("/jobs/{job_id}/status")
async def job_status(job_id: str):
    job = _get_job(job_id)
    d = job.to_dict()
    with job._lock:
        d["recent_logs"] = job.logs[-50:]
    return d

@app.get("/jobs/{job_id}/logs")
async def stream_logs(job_id: str):
    job = _get_job(job_id)
    async def generate():
        sent = 0
        while True:
            with job._lock:
                new_lines      = job.logs[sent:]
                current_status = job.status
            for line in new_lines:
                yield f"data: {line}\n\n"
            sent += len(new_lines)
            if current_status in ("done", "error") and not new_lines:
                yield f"data: [STREAM END — job {current_status}]\n\n"
                break
            await asyncio.sleep(0.5)
    return StreamingResponse(generate(), media_type="text/event-stream")

@app.get("/jobs/{job_id}/results")
async def download_results(job_id: str):
    job = _get_job(job_id)
    results_dir = job.work_dir / "sufi2_results"
    if not results_dir.exists():
        raise HTTPException(404, "Results directory not found.")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for f in results_dir.rglob("*"):
            if f.is_file():
                zf.write(f, f.relative_to(job.work_dir))
    buf.seek(0)
    return StreamingResponse(buf, media_type="application/zip",
        headers={"Content-Disposition": f"attachment; filename=sufi2_results_{job_id}.zip"})

@app.get("/jobs/{job_id}/files")
async def list_result_files(job_id: str):
    """List all result files with their sizes."""
    job = _get_job(job_id)
    results_dir = job.work_dir / "sufi2_results"
    if not results_dir.exists():
        return {"files": []}
    files = []
    for f in sorted(results_dir.rglob("*")):
        if f.is_file():
            files.append({
                "path":     str(f.relative_to(results_dir)),
                "size_kb":  round(f.stat().st_size / 1024, 1),
                "folder":   f.parent.name if f.parent != results_dir else "root",
            })
    return {"files": files, "total": len(files)}

@app.delete("/jobs/{job_id}", status_code=204)
async def delete_job(job_id: str):
    job = _get_job(job_id)
    if job.status == "running":
        raise HTTPException(409, "Cannot delete a running job.")
    if job.scratch and job.scratch.exists():
        shutil.rmtree(job.scratch, ignore_errors=True)
    with JOBS_LOCK:
        JOBS.pop(job_id, None)

# ─────────────────────────────────────────────────────────────────────────────
# Background runners
# ─────────────────────────────────────────────────────────────────────────────

def _run_phase(job: Job, cfg: SUFI2Config, phase: str):
    from sufi2.core import SUFI2Engine
    job.status = "running"; job.phase = phase
    job.emit(f"Phase: {phase}")

    def progress(msg, pct):
        job.emit(msg, pct)

    try:
        engine = SUFI2Engine(cfg, progress_callback=progress)
        job._engine = engine

        if phase == "calibration":
            result = engine.calibrate()
        elif phase == "validation":
            result = engine.validate()
        elif phase == "hindcast":
            result = engine.run_hindcast()
        elif phase == "forecast":
            result = engine.run_forecast()
        else:
            raise ValueError(f"Unknown phase: {phase}")

        job.result = _sanitise(result)
        job.status = "done"
        job.emit(f"{phase.capitalize()} complete ✓", 1.0)
        gc.collect()

    except Exception as e:
        job.error  = str(e)
        job.status = "error"
        job.emit(f"ERROR: {e}")
        from sufi2.logger import log_exception
        log_exception(log, f"Job {job.job_id} phase {phase} failed", e)

def _run_phase_existing(job: Job, phase: str):
    """Run a subsequent phase using the existing engine (preserves best params)."""
    job.status = "running"; job.phase = phase
    job.emit(f"Phase: {phase}")

    def progress(msg, pct):
        job.emit(msg, pct)

    try:
        engine = job._engine
        engine._emit = progress

        if phase == "validation":
            result = engine.validate()
        elif phase == "hindcast":
            result = engine.run_hindcast()
        elif phase == "forecast":
            result = engine.run_forecast()
        else:
            raise ValueError(f"Unknown phase: {phase}")

        job.result = _sanitise(result)
        job.status = "done"
        job.emit(f"{phase.capitalize()} complete ✓", 1.0)
        gc.collect()

    except Exception as e:
        job.error  = str(e)
        job.status = "error"
        job.emit(f"ERROR: {e}")
        from sufi2.logger import log_exception
        log_exception(log, f"Job {job.job_id} phase {phase} failed", e)
