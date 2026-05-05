# SUFI-2 SWAT Calibration Tool

A fully packaged, user-configurable SUFI-2 multi-reach SWAT calibration tool.
Runs as a **CLI**, **REST API**, or **Web UI** — all containerized in Docker.

---

## Quick Start (Docker — recommended)

### 1. Clone / copy this folder

```bash
git clone <your-repo> sufi2_tool
cd sufi2_tool
```

### 2. Put your SWAT files in `./data/`

```
data/
├── file.cio
├── output.rch
├── observed_flow_rch13.csv
├── observed_flow_rch15.csv
├── observed_flow_rch16.csv
├── observed_flow_rch17.csv
├── *.mgt  *.gw  *.sol  *.rte  *.hru  *.bsn
└── swat2012.exe  (optional — mock runner used if absent)
```

Each observed flow CSV must have two columns: `date` (YYYY-MM-DD) and `flow` (m³/s).

### 3. Scaffold your config

```bash
docker compose run --rm cli init --output /data/config.yaml
```

Edit `./data/config.yaml` — set your periods, iterations, objective function, etc.

### 4. Validate

```bash
docker compose run --rm cli validate --config /data/config.yaml
```

### 5a. Run via CLI

```bash
docker compose run --rm cli run --config /data/config.yaml
```

### 5b. Run via Web UI

```bash
docker compose up
# Open http://localhost:8000
```

Fill in the form and click **Run Calibration**. Logs stream live; download results as ZIP when done.

### 5c. Run via API

```bash
# Start the server
docker compose up -d

# Submit a job
curl -X POST http://localhost:8000/run \
  -F "config_file=@./data/config.yaml"

# Poll status
curl http://localhost:8000/status/<job_id>

# Download results
curl -O http://localhost:8000/results/<job_id>
```

---

## Local Install (without Docker)

```bash
pip install -e .
sufi2 --help
sufi2 init
sufi2 validate --config config.yaml
sufi2 run --config config.yaml
```

Start the API server:

```bash
uvicorn sufi2.api:app --host 0.0.0.0 --port 8000
```

---

## Config Reference (`config.yaml`)

| Key | Default | Description |
|-----|---------|-------------|
| `paths.work_dir` | `.` | Directory with SWAT files |
| `paths.results_dir` | `sufi2_results` | Output directory |
| `paths.swat_exe` | `null` | Path to SWAT exe (auto-detect if null) |
| `paths.par_inf` | `null` | Parameter ranges file (built-in defaults if null) |
| `calibration.iterations` | `3` | SUFI-2 iterations |
| `calibration.n_samples` | `500` | LHS samples per iteration |
| `calibration.obj_func` | `NSE` | `NSE` \| `KGE` \| `R2` |
| `calibration.top_fraction` | `0.10` | Top fraction for range update |
| `calibration.warmup_years` | `3` | Warm-up years excluded from scoring |
| `calibration.seed` | `42` | Random seed |
| `calibration.reach_weights` | `null` | Per-reach weights (must sum to 1.0) |
| `periods.calibration.start/end` | `1993-01-01 / 2001-12-31` | Calibration window |
| `periods.validation.start/end` | `2002-01-01 / 2005-12-31` | Validation window |
| `periods.hindcast` | `null` | Optional hindcast period |
| `periods.forecast` | `null` | Optional forecast period |
| `reach_ids` | `null` | Reaches to calibrate (auto-detect if null) |
| `backup_inputs` | `true` | Backup SWAT input files before modification |

---

## Output Files

```
sufi2_results/
├── best_parameters.csv          ← best parameter set across all iterations
├── summary_report.txt           ← plain-text calibration summary
├── convergence.png              ← NSE convergence box-plot
├── input_backup/                ← original SWAT input files (if backup_inputs: true)
└── iteration_N/
    ├── parameter_sets.csv       ← all LHS samples + objective scores
    ├── sensitivity_combined.csv ← t-stat / p-value (combined objective)
    ├── sensitivity_rchN.csv     ← per-reach sensitivity
    ├── ppu_band_rchN.csv        ← 95PPU band per reach
    ├── updated_ranges.csv       ← narrowed ranges for next iteration
    ├── dotty_combined.png
    ├── dotty_rchN.png
    ├── 95ppu_rchN.png
    └── sensitivity_rchN.png
```

---

## API Endpoints

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/` | Web UI |
| `GET` | `/health` | Health check |
| `POST` | `/run` | Start calibration job |
| `GET` | `/status/{id}` | Job status + recent logs |
| `GET` | `/logs/{id}` | Live log stream (SSE) |
| `GET` | `/results/{id}` | Download results ZIP |
| `GET` | `/jobs` | List all jobs |
| `DELETE` | `/jobs/{id}` | Delete job + temp files |
| `GET` | `/docs` | Interactive API docs (Swagger) |

---

## Parameter Change Types

| Type | Formula | Use for |
|------|---------|---------|
| `v` | replace with new value | Absolute parameters (ALPHA_BF, ESCO…) |
| `r` | `original × (1 + new_val)` | Relative change (CN2, SOL_AWC…) |
| `a` | `original + new_val` | Additive adjustment |

---

## Requirements

- Python 3.10+
- numpy, pandas, scipy, matplotlib, pydantic, pyyaml, click, fastapi, uvicorn
- SWAT 2012 or 2020 executable (optional — mock runner used if absent)
