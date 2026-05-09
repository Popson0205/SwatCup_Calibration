"""
sufi2.api — FastAPI application for Hydro-Modeller (SUFI-2 SWAT Calibration).

Endpoints:
  POST /jobs/calibrate          — start calibration job
  POST /jobs/{id}/validate      — run validation with best params
  POST /jobs/{id}/hindcast      — run hindcast
  POST /jobs/{id}/forecast      — run forecast
  POST /jobs/{id}/cancel        — cancel running job
  POST /jobs/{id}/restart       — restart from scratch
  GET  /jobs/{id}/status        — job status + logs
  GET  /jobs/{id}/logs          — SSE live log stream
  GET  /jobs/{id}/results       — download results ZIP
  GET  /jobs/{id}/files         — list result files
  GET  /jobs                    — list all jobs
  GET  /parameters              — built-in parameter catalogue (cached)
  GET  /scan                    — scan server-side directory
  GET  /health                  — health check
  GET  /                        — Web UI
"""
from __future__ import annotations

import asyncio, gc, io, json, logging, math, os, shutil, tempfile, threading, time, uuid, zipfile
from pathlib import Path
from typing import Dict, List, Optional

import yaml
from fastapi import BackgroundTasks, FastAPI, HTTPException, Request, UploadFile, File, Form
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles

from sufi2.config import SUFI2Config
from sufi2.logger import get_logger, read_log_tail, get_log_path
from sufi2 import db as _db

log = get_logger("sufi2.api")

# ── Constants ─────────────────────────────────────────────────────────────────
MAX_UPLOAD_MB   = int(os.getenv("SUFI2_MAX_UPLOAD_MB", "500"))
JOB_TTL_HOURS   = int(os.getenv("SUFI2_JOB_TTL_HOURS", "2"))
DEV_MODE        = os.getenv("SUFI2_DEV_MODE", "").lower() in ("1", "true", "yes")

# ── Helpers ───────────────────────────────────────────────────────────────────
def _sanitise(obj):
    """Recursively replace inf/nan with None so JSON serialisation never fails."""
    if isinstance(obj, float):
        return None if (math.isnan(obj) or math.isinf(obj)) else obj
    if isinstance(obj, dict):
        return {k: _sanitise(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_sanitise(v) for v in obj]
    return obj

def _nse_rating(nse: float) -> str:
    if nse is None or math.isnan(nse): return "N/A"
    if nse >= 0.75: return "Very Good"
    if nse >= 0.65: return "Good"
    if nse >= 0.50: return "Satisfactory"
    return "Unsatisfactory"

# ── Job store ─────────────────────────────────────────────────────────────────
class Job:
    def __init__(self, job_id: str, work_dir: Path, scratch: Optional[Path] = None):
        self.job_id     = job_id
        self.work_dir   = work_dir
        self.scratch    = scratch
        self.status     = "queued"
        self.phase      = "calibration"
        self.progress   = 0.0
        self.logs: List[str] = []
        self.milestones: List[dict] = []
        self.result: Optional[dict] = None
        self.error:  Optional[str]  = None
        self.created_at = time.time()
        self._lock      = threading.Lock()
        self._engine    = None
        self._cancelled = False

    def emit(self, msg: str, pct: float = 0.0):
        with self._lock:
            self.progress = pct
            self.logs.append(f"[{time.strftime('%H:%M:%S')}] {msg}")
            # Extract milestones from structured messages
            if "✓" in msg or "complete" in msg.lower() or "best" in msg.lower():
                self.milestones.append({"time": time.strftime('%H:%M:%S'), "msg": msg, "pct": round(pct*100,1)})

    def to_dict(self) -> dict:
        with self._lock:
            return {
                "job_id":     self.job_id,
                "status":     self.status,
                "phase":      self.phase,
                "progress":   round(self.progress * 100, 1),
                "log_lines":  len(self.logs),
                "milestones": self.milestones[-10:],
                "result":     self.result,
                "error":      self.error,
                "created_at": self.created_at,
            }

JOBS: Dict[str, Job] = {}
JOBS_LOCK = threading.Lock()

# Cached parameters (loaded once)
_PARAMS_CACHE: Optional[dict] = None
_PARAMS_LOCK  = threading.Lock()

# ── App ───────────────────────────────────────────────────────────────────────
app = FastAPI(
    title="Hydro-Modeller API",
    description="SUFI-2 SWAT Multi-Reach Calibration — by Idris Popoola Bamigboye",
    version="2.1.0",
)

app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

@app.middleware("http")
async def security_and_size(request: Request, call_next):
    # Request size limit (uploads)
    if request.method == "POST":
        content_length = request.headers.get("content-length")
        if content_length and int(content_length) > MAX_UPLOAD_MB * 1024 * 1024:
            return Response(
                content=json.dumps({"detail": f"Upload too large. Maximum is {MAX_UPLOAD_MB} MB."}),
                status_code=413, media_type="application/json"
            )
    response = await call_next(request)
    response.headers["X-Frame-Options"]           = "SAMEORIGIN"
    response.headers["X-Content-Type-Options"]    = "nosniff"
    response.headers["X-Powered-By"]              = "Hydro-Modeller"
    response.headers["Content-Security-Policy"]   = (
        "default-src 'self' https://cdn.plot.ly; "
        "script-src 'self' 'unsafe-inline' https://cdn.plot.ly; "
        "style-src 'self' 'unsafe-inline'; "
        "img-src 'self' data: blob:;"
    )
    return response

# ── Background job cleanup ────────────────────────────────────────────────────
async def _cleanup_old_jobs():
    """Purge jobs older than JOB_TTL_HOURS to prevent memory leaks."""
    while True:
        await asyncio.sleep(1800)  # check every 30 min
        cutoff = time.time() - JOB_TTL_HOURS * 3600
        to_delete = []
        with JOBS_LOCK:
            for jid, job in JOBS.items():
                if job.created_at < cutoff and job.status not in ("running", "queued"):
                    to_delete.append(jid)
        for jid in to_delete:
            job = JOBS.get(jid)
            if job and job.scratch and job.scratch.exists():
                shutil.rmtree(job.scratch, ignore_errors=True)
            with JOBS_LOCK:
                JOBS.pop(jid, None)
            log.info(f"Purged old job {jid}")

@app.on_event("startup")
async def startup():
    asyncio.create_task(_cleanup_old_jobs())
    _db.init_db()
    log.info(f"Hydro-Modeller API v2.1.0 started | DEV_MODE={DEV_MODE} | MAX_UPLOAD={MAX_UPLOAD_MB}MB | JOB_TTL={JOB_TTL_HOURS}h | DB={_db.db_status().get('mode','unknown')}")

_UI_DIR = Path(__file__).parent / "ui"
if _UI_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(_UI_DIR)), name="static")

# ── Core routes ───────────────────────────────────────────────────────────────
@app.api_route("/", methods=["GET", "HEAD"], response_class=HTMLResponse, include_in_schema=False)
async def serve_ui(request: Request):
    ui_file = _UI_DIR / "index.html"
    body = ui_file.read_text(encoding="utf-8") if ui_file.exists() else \
           "<h1>Hydro-Modeller</h1><p>UI not found. See <a href='/docs'>/docs</a>.</p>"
    if request.method == "HEAD":
        return Response(status_code=200, headers={"content-type": "text/html"})
    return HTMLResponse(content=body)

@app.api_route("/health", methods=["GET", "HEAD"])
async def health(request: Request):
    if request.method == "HEAD":
        return Response(status_code=200)
    with JOBS_LOCK:
        active = sum(1 for j in JOBS.values() if j.status == "running")
        total  = len(JOBS)
    return {"status": "ok", "version": "2.1.0", "jobs": {"active": active, "total": total}, "database": _db.db_status()}

@app.get("/parameters")
async def get_parameters():
    """Full SWAT parameter catalogue — cached after first load."""
    global _PARAMS_CACHE
    with _PARAMS_LOCK:
        if _PARAMS_CACHE is None:
            from sufi2.core import DEFAULT_PARAMS
            df = DEFAULT_PARAMS.copy()
            _PARAMS_CACHE = {
                "parameters": df.to_dict(orient="records"),
                "categories": sorted(df["category"].unique().tolist()) if "category" in df.columns else [],
                "top5": ["CN2.mgt", "ALPHA_BF.gw", "GW_DELAY.gw", "CH_N2.rte", "ESCO.bsn"],
                "change_types": {
                    "v": "Replace — set parameter directly to new value",
                    "r": "Relative — original × (1 + new_val)",
                    "a": "Additive — original + new_val",
                },
            }
    return _PARAMS_CACHE

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
        result["errors"].append("output.rch not found — run SWAT once first to generate it")
    if not result["files"]["observed_flow"]["found"]:
        result["errors"].append("No observed_flow_rchN.csv files found")
    if not result["files"]["file_cio"]["found"]:
        result["warnings"].append("file.cio not found — date parsing may be limited")
    if not result["files"]["swat_exe"]["found"]:
        result["warnings"].append("SWAT executable not found — mock runner will be used for testing")
    if not result["files"]["par_inf"]["found"]:
        result["warnings"].append("par_inf.txt not found — built-in 71-parameter defaults will be used")

    result["ready"] = len(result["errors"]) == 0
    return result

@app.get("/logs/file")
async def get_log_file(lines: int = 200):
    return {"log_path": get_log_path(), "lines": lines, "content": read_log_tail(lines)}

# ── Job management ────────────────────────────────────────────────────────────
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
    """Parse config, handle file uploads, validate, return (job_id, job, cfg)."""
    data = await _parse_request(config_file, config_json)
    selected_params = data.pop("_selected_params", None)

    # Validate total upload size
    total_size = 0
    if data_files:
        for uf in data_files:
            content = await uf.read()
            total_size += len(content)
            await uf.seek(0)
        if total_size > MAX_UPLOAD_MB * 1024 * 1024:
            raise HTTPException(413, f"Total upload size exceeds {MAX_UPLOAD_MB} MB limit.")

    try:
        cfg = SUFI2Config.from_dict(data)
    except Exception as e:
        raise HTTPException(422, f"Config validation error: {e}")

    job_id  = str(uuid.uuid4())[:8]
    scratch = None

    if data_files:
        scratch = Path(tempfile.mkdtemp(prefix=f"sufi2_{job_id}_"))
        for uf in data_files:
            raw = await uf.read()
            if raw:
                (scratch / uf.filename).write_bytes(raw)
        effective_work_dir = scratch
        cfg = cfg.model_copy(update={"paths": cfg.paths.model_copy(update={"work_dir": str(scratch)})})
    else:
        effective_work_dir = cfg.paths.work_path
        if not effective_work_dir.exists():
            raise HTTPException(422, f"work_dir not found: {effective_work_dir}. Upload files instead.")

    if selected_params:
        par_lines = ["# par_inf.txt — generated by Hydro-Modeller"]
        for p in selected_params:
            par_lines.append(f"{p['name']:<20} {p['min']:>12} {p['max']:>12}  {p['change_type']}")
        par_path = effective_work_dir / "par_inf.txt"
        par_path.write_text("\n".join(par_lines) + "\n")
        cfg = cfg.model_copy(update={"paths": cfg.paths.model_copy(update={"par_inf": str(par_path)})})

    # Backup input files into results dir so they're included in ZIP
    job = Job(job_id=job_id, work_dir=effective_work_dir, scratch=scratch)
    with JOBS_LOCK:
        JOBS[job_id] = job

    return job_id, job, cfg

# ── Phase endpoints ───────────────────────────────────────────────────────────
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
    job = _get_job(job_id)
    if job.status == "running": raise HTTPException(409, "Job is already running.")
    if job._engine is None:    raise HTTPException(409, "Calibration must complete before validation.")
    background_tasks.add_task(_run_phase_existing, job, "validation")
    return {"job_id": job_id, "phase": "validation", "status": "queued"}

@app.post("/jobs/{job_id}/hindcast", status_code=202)
async def start_hindcast(job_id: str, background_tasks: BackgroundTasks):
    job = _get_job(job_id)
    if job.status == "running": raise HTTPException(409, "Job is already running.")
    if job._engine is None:    raise HTTPException(409, "Calibration must complete before hindcast.")
    background_tasks.add_task(_run_phase_existing, job, "hindcast")
    return {"job_id": job_id, "phase": "hindcast", "status": "queued"}

@app.post("/jobs/{job_id}/forecast", status_code=202)
async def start_forecast(job_id: str, background_tasks: BackgroundTasks):
    job = _get_job(job_id)
    if job.status == "running": raise HTTPException(409, "Job is already running.")
    if job._engine is None:    raise HTTPException(409, "Calibration must complete before forecast.")
    background_tasks.add_task(_run_phase_existing, job, "forecast")
    return {"job_id": job_id, "phase": "forecast", "status": "queued"}

@app.post("/jobs/{job_id}/cancel")
async def cancel_job(job_id: str):
    job = _get_job(job_id)
    with job._lock:
        if job.status not in ("queued", "running"):
            raise HTTPException(400, f"Job is {job.status} — cannot cancel.")
        job.status     = "cancelled"
        job._cancelled = True
        job.logs.append(f"[{time.strftime('%H:%M:%S')}] ⛔ Job cancelled by user.")
        if job._engine:
            try: job._engine._cancelled = True
            except Exception: pass
    return {"status": "cancelled", "job_id": job_id}

@app.post("/jobs/{job_id}/restart")
async def restart_job(job_id: str, background_tasks: BackgroundTasks):
    job = _get_job(job_id)
    if job.status == "running":
        raise HTTPException(400, "Job is still running — cancel it first.")
    cfg_path = job.work_dir / "_job_config.json"
    if not cfg_path.exists():
        raise HTTPException(400, "Original config not found — please submit a new job.")
    with job._lock:
        job.status     = "queued"
        job.progress   = 0.0
        job._cancelled = False
        job.logs       = [f"[{time.strftime('%H:%M:%S')}] 🔄 Job restarted — previous results preserved."]
        job.milestones = []
        job.result     = None
        job.error      = None
        job.phase      = "calibration"
        job._engine    = None
        # Do NOT delete previous results — each run gets its own timestamped folder
    cfg_data = json.loads(cfg_path.read_text())
    cfg = SUFI2Config(**cfg_data)
    background_tasks.add_task(_run_phase, job, cfg, "calibration")
    return {"status": "queued", "job_id": job_id}

@app.get("/jobs/{job_id}/status")
async def job_status(job_id: str):
    job = _get_job(job_id)
    d = job.to_dict()
    with job._lock:
        d["recent_logs"] = job.logs[-50:]
        # Add performance ratings to result if available
        if job.result and "reach_metrics" in (job.result or {}):
            metrics = job.result["reach_metrics"]
            d["performance_summary"] = {
                rid: {
                    **m,
                    "nse_rating":   _nse_rating(m.get("NSE", float("nan"))),
                }
                for rid, m in metrics.items()
            }
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
            if current_status in ("done", "error", "cancelled") and not new_lines:
                yield f"data: [STREAM END — job {current_status}]\n\n"
                break
            await asyncio.sleep(0.5)
    return StreamingResponse(generate(), media_type="text/event-stream")

@app.get("/jobs/{job_id}/results")
async def download_results(job_id: str):
    job = _get_job(job_id)
    results_dir = job.work_dir / "sufi2_results"
    if not results_dir.exists():
        raise HTTPException(404, "Results directory not found. Run calibration first.")
    buf = io.BytesIO()
    SKIP_EXTS = {".py", ".bak"}
    input_exts = {".rch", ".cio", ".sub", ".hru", ".bsn", ".gw", ".mgt",
                  ".sol", ".rte", ".pnd", ".wwq", ".csv", ".txt"}
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        # Include ALL run folders (preserves full history)
        for f in results_dir.rglob("*"):
            if f.is_file() and f.suffix not in SKIP_EXTS:
                zf.write(f, f.relative_to(job.work_dir))
        # Include input files
        for f in job.work_dir.iterdir():
            if f.is_file() and f.suffix.lower() in input_exts and not f.name.startswith("_"):
                zf.write(f, Path("input_files") / f.name)
    buf.seek(0)
    return StreamingResponse(buf, media_type="application/zip",
        headers={"Content-Disposition": f"attachment; filename=HydroModeller_results_{job_id}.zip"})

@app.get("/jobs/{job_id}/image")
async def get_result_image(job_id: str, path: str, run: str = "latest"):
    """Serve a chart file (PNG or JSON). Use run=latest or run=run_YYYYMMDD_HHMMSS."""
    from fastapi.responses import FileResponse as FR
    job = _get_job(job_id)
    base = job.work_dir / "sufi2_results"

    if run == "latest":
        run_dirs = sorted([d for d in base.iterdir()
                          if d.is_dir() and d.name.startswith("run_")], reverse=True) if base.exists() else []
        results_dir = run_dirs[0] if run_dirs else base
    else:
        results_dir = base / run if (base / run).exists() else base

    img_path = (results_dir / path).resolve()
    try:
        img_path.relative_to(job.work_dir.resolve())
    except ValueError:
        raise HTTPException(403, "Access denied")
    if not img_path.exists():
        raise HTTPException(404, f"File not found: {path}")
    if img_path.suffix == ".json":
        return FR(str(img_path), media_type="application/json")
    return FR(str(img_path), media_type="image/png")

@app.get("/jobs/{job_id}/runs")
async def list_runs(job_id: str):
    """List all calibration runs for this job (each run is a timestamped folder)."""
    job = _get_job(job_id)
    base = job.work_dir / "sufi2_results"
    if not base.exists():
        return {"runs": []}
    runs = []
    for d in sorted(base.iterdir(), reverse=True):
        if d.is_dir() and d.name.startswith("run_"):
            # Count files and find best NSE
            files = list(d.rglob("*"))
            nse_val = None
            metrics_f = d / "calibration" / "calibration_metrics.csv"
            if not metrics_f.exists():
                # Try finding any metrics file
                for mf in d.rglob("*metrics*.csv"):
                    metrics_f = mf; break
            if metrics_f.exists():
                try:
                    import pandas as pd
                    mdf = pd.read_csv(metrics_f)
                    if "NSE" in mdf.columns:
                        nse_val = round(float(mdf["NSE"].max()), 3)
                except Exception:
                    pass
            runs.append({
                "run_id":    d.name,
                "timestamp": d.name.replace("run_",""),
                "files":     len([f for f in files if f.is_file()]),
                "best_nse":  nse_val,
                "has_validation": (d / "validation").exists(),
            })
    return {"runs": runs, "total": len(runs)}

@app.get("/jobs/{job_id}/files")
async def list_result_files(job_id: str, run: str = "latest"):
    """List result files. Use run=latest for most recent, or run=run_YYYYMMDD_HHMMSS."""
    job = _get_job(job_id)
    base = job.work_dir / "sufi2_results"
    if not base.exists():
        return {"files": [], "total": 0, "run": None}

    # Resolve run folder
    if run == "latest":
        run_dirs = sorted([d for d in base.iterdir()
                          if d.is_dir() and d.name.startswith("run_")], reverse=True)
        results_dir = run_dirs[0] if run_dirs else base
    else:
        results_dir = base / run
        if not results_dir.exists():
            results_dir = base  # fallback

    files = []
    for f in sorted(results_dir.rglob("*")):
        if f.is_file():
            files.append({
                "path":    str(f.relative_to(results_dir)),
                "size_kb": round(f.stat().st_size / 1024, 1),
                "folder":  f.parent.name if f.parent != results_dir else "root",
            })
    return {"files": files, "total": len(files), "run": results_dir.name}

# ── Project persistence (Neon DB) ────────────────────────────────────────────

@app.get("/projects")
async def list_projects():
    """List all saved projects from the database."""
    db_projects = _db.list_projects_db()
    return {"projects": db_projects, "storage": "database" if db_projects is not None else "local"}

@app.post("/projects/{name}")
async def save_project(name: str, request: Request):
    """Save a project to the database."""
    try:
        config = await request.json()
    except Exception:
        raise HTTPException(422, "Invalid JSON body")
    ok = _db.save_project_db(name, config)
    if not ok:
        raise HTTPException(503, "Database not available — save locally instead")
    return {"saved": True, "name": name}

@app.get("/projects/{name}")
async def load_project(name: str):
    """Load a project from the database."""
    config = _db.load_project_db(name)
    if config is None:
        raise HTTPException(404, f"Project '{name}' not found in database")
    return {"name": name, "config": config}

@app.delete("/projects/{name}", status_code=204)
async def delete_project(name: str):
    """Delete a project from the database."""
    _db.delete_project_db(name)

@app.get("/history")
async def job_history(limit: int = 20):
    """Return job history from the database."""
    return {"jobs": _db.list_jobs_from_db(limit=limit)}

@app.delete("/jobs/{job_id}", status_code=204)
async def delete_job(job_id: str):
    job = _get_job(job_id)
    if job.status == "running":
        raise HTTPException(409, "Cannot delete a running job.")
    if job.scratch and job.scratch.exists():
        shutil.rmtree(job.scratch, ignore_errors=True)
    with JOBS_LOCK:
        JOBS.pop(job_id, None)

# ── Background runners ────────────────────────────────────────────────────────
def _run_phase(job: Job, cfg: SUFI2Config, phase: str):
    from sufi2.core import SUFI2Engine
    job.status = "running"
    job.phase  = phase

    # Generate timestamped run folder so each calibration run is preserved
    run_ts = time.strftime("%Y%m%d_%H%M%S")
    run_results_dir = f"sufi2_results/run_{run_ts}"
    try:
        cfg = cfg.model_copy(update={
            "paths": cfg.paths.model_copy(update={"results_dir": run_results_dir})
        })
    except Exception:
        pass  # use default if model_copy fails

    # Save config for restart
    try:
        (job.work_dir / "_job_config.json").write_text(
            json.dumps(cfg.model_dump(), default=str)
        )
    except Exception:
        pass

    # Persist job to database
    _db.save_job(job.job_id, "running", phase, job.created_at,
                 config=cfg.model_dump(mode="json"))

    def progress(msg, pct):
        job.emit(msg, pct)
        if job._cancelled:
            raise InterruptedError("Job cancelled by user")

    try:
        engine = SUFI2Engine(cfg, progress_callback=progress)
        job._engine = engine
        result = engine.calibrate()
        job.result = _sanitise(result)
        job.status = "done"
        job.emit("Calibration complete ✓", 1.0)
        # Persist to database
        _db.save_job(job.job_id, "done", phase, job.created_at, result=job.result)
        if job.result and "reach_metrics" in (job.result or {}):
            _db.save_results(job.job_id, phase, job.result["reach_metrics"])
        gc.collect()
    except InterruptedError:
        job.status = "cancelled"
        _db.save_job(job.job_id, "cancelled", phase, job.created_at)
    except Exception as e:
        job.error  = str(e)
        job.status = "error"
        job.emit(f"ERROR: {e}")
        _db.save_job(job.job_id, "error", phase, job.created_at, error=str(e))
        from sufi2.logger import log_exception
        log_exception(log, f"Job {job.job_id} calibration failed", e)

def _run_phase_existing(job: Job, phase: str):
    job.status = "running"
    job.phase  = phase

    def progress(msg, pct):
        job.emit(msg, pct)
        if job._cancelled:
            raise InterruptedError("Job cancelled by user")

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
    except InterruptedError:
        job.status = "cancelled"
    except Exception as e:
        job.error  = str(e)
        job.status = "error"
        job.emit(f"ERROR: {e}")
        from sufi2.logger import log_exception
        log_exception(log, f"Job {job.job_id} phase {phase} failed", e)
