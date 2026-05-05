# SUFI-2 SWAT Calibration Tool

[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Python 3.10+](https://img.shields.io/badge/python-3.10+-blue.svg)](https://python.org)
[![FastAPI](https://img.shields.io/badge/FastAPI-0.110+-green.svg)](https://fastapi.tiangolo.com)

A professional, open-source multi-reach SWAT calibration tool implementing the
**SUFI-2 (Sequential Uncertainty Fitting)** algorithm. Runs as a CLI, REST API,
or browser-based Web UI — deployable on Render, Docker, or locally.

---

## Features

- **Multi-reach calibration** — calibrate any number of reaches simultaneously
- **Flexible reach selection** — upload all observed flow files, specify only the reaches you want
- **Sequential workflow** — Setup → Calibration → Validation → Hindcast/Forecast → Results
- **In-app visualisation** — 95PPU plots, sensitivity charts, dotty plots, convergence — all per reach
- **Project management** — save/load/export/import configurations as JSON
- **Live progress** — year-by-year simulation progress, live NSE scoring per sample
- **Both CSV formats** — narrow (`date, flow`) or wide (`date, rch13, rch15, ...`)
- **Robust date parsing** — reads `file.cio` IYR/NYSKIP to correctly assign output.rch dates

---

## Quick Start (Render / Cloud)

1. Deploy to [Render](https://render.com) using the included `render.yaml`
2. Open the app URL
3. Click **Select files from TxtInOut folder** → `Ctrl+A` → Open
4. Fill in calibration settings and periods
5. Click **▶ Start Calibration**

---

## Local Install

```bash
pip install -e .
uvicorn sufi2.api:app --host 0.0.0.0 --port 8000
# Open http://localhost:8000
```

## CLI Usage

```bash
sufi2 init                              # scaffold config.yaml
sufi2 validate --config config.yaml    # check files & config
sufi2 run --config config.yaml         # run calibration
```

---

## Required Input Files

| File | Description |
|------|-------------|
| `output.rch` | SWAT simulated reach output (run SWAT once first) |
| `file.cio` | Master watershed file (provides simulation dates) |
| `observed_flow_rch<N>.csv` | Observed flow per reach — columns: `date, flow` |
| `*.mgt .gw .sol .hru .rte .bsn` | SWAT input files (modified during calibration) |
| `par_inf.txt` | Parameter ranges (optional — built-in defaults used if absent) |
| `swat2012.exe` / `swat_rel` | SWAT executable (optional — mock runner used if absent) |

**Observed flow date format:** `Jan 1, 1993` or `1993-01-01` — both accepted.

---

## Output Structure

```
sufi2_results/
├── best_parameters.csv          ← best parameter set
├── calibration/
│   ├── convergence.png
│   └── iteration_N/
│       ├── parameter_sets.csv
│       ├── sensitivity_rch<N>.csv / .png
│       ├── ppu_band_rch<N>.csv
│       ├── 95ppu_rch<N>.png
│       └── dotty_rch<N>.png
├── validation/
│   ├── validation_metrics.csv
│   ├── validation_flow_rch<N>.csv
│   └── validation_rch<N>.png
├── hindcast/
│   └── hindcast_flow_rch<N>.csv
└── forecast/
    └── forecast_flow_rch<N>.csv
```

---

## Configuration Reference

```yaml
paths:
  work_dir: "."
  results_dir: "sufi2_results"
  swat_exe: null          # auto-detect or null for mock runner
  par_inf: null           # null = built-in 20-parameter defaults

calibration:
  iterations: 3
  n_samples: 500
  obj_func: NSE           # NSE | KGE | R2
  top_fraction: 0.10
  warmup_years: 3
  seed: 42
  reach_weights: null     # null = equal weights

periods:
  calibration: {start: "1993-01-01", end: "2000-12-31"}
  validation:  {start: "2001-01-01", end: "2005-12-31"}
  hindcast:    {start: "1986-01-01", end: "1992-12-31"}  # optional
  forecast:    {start: "2006-01-01", end: "2024-12-31"}  # optional

reach_ids: [16]           # null = all detected reaches
backup_inputs: true
```

---

## Deploying on Render (Free Tier)

1. Push repo to GitHub
2. Render auto-detects `render.yaml`
3. Build: `pip install -e .`
4. Start: `uvicorn sufi2.api:app --host 0.0.0.0 --port $PORT`

> **Note:** Free tier sleeps after 15 min inactivity. The app shows a wake-up
> screen while it restarts (~30s). Upgrade to a paid plan for always-on hosting.

---

## License

MIT — see [LICENSE](LICENSE)
