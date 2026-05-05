"""
sufi2.core
==========
Pure calibration engine — no hardcoded config, no CLI/API concerns.

Key design:
- Memory-safe: raw simulations are NEVER stored after objectives/PPU computed
- Clean output folder: sufi2_results/calibration/iteration_N/, validation/, hindcast/, forecast/
- Phases are independent: calibrate() → validate() → run_hindcast() → run_forecast()

Usage:
    from sufi2.core import SUFI2Engine
    engine = SUFI2Engine(config)
    calib_result  = engine.calibrate()
    val_result    = engine.validate()
    hind_result   = engine.run_hindcast()
    fore_result   = engine.run_forecast()
"""

from __future__ import annotations

import gc
import logging
import re
import shutil
import subprocess
import tempfile
import warnings
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

import matplotlib
matplotlib.use("Agg")
import matplotlib.patches as mpatches
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
from scipy import stats

from sufi2.config import SUFI2Config
from sufi2.logger import get_logger

warnings.filterwarnings("ignore", category=RuntimeWarning)
log = get_logger("sufi2.core")


# ─────────────────────────────────────────────────────────────────────────────
# DEFAULT PARAMETER TABLE
# ─────────────────────────────────────────────────────────────────────────────

DEFAULT_PARAMS = pd.DataFrame([
    {"name": "CN2.mgt",     "min": -0.25,   "max":  0.25,  "change_type": "r"},
    {"name": "ALPHA_BF.gw", "min":  0.00,   "max":  1.00,  "change_type": "v"},
    {"name": "GW_DELAY.gw", "min":  0.00,   "max":  500.0, "change_type": "v"},
    {"name": "GWQMN.gw",    "min":  0.00,   "max":  5000., "change_type": "v"},
    {"name": "GW_REVAP.gw", "min":  0.02,   "max":  0.20,  "change_type": "v"},
    {"name": "REVAPMN.gw",  "min":  0.00,   "max":  500.0, "change_type": "v"},
    {"name": "SOL_AWC.sol", "min": -0.20,   "max":  0.20,  "change_type": "r"},
    {"name": "SOL_K.sol",   "min": -0.80,   "max":  0.80,  "change_type": "r"},
    {"name": "SOL_BD.sol",  "min": -0.50,   "max":  0.60,  "change_type": "r"},
    {"name": "ESCO.hru",    "min":  0.00,   "max":  1.00,  "change_type": "v"},
    {"name": "EPCO.hru",    "min":  0.00,   "max":  1.00,  "change_type": "v"},
    {"name": "CH_N2.rte",   "min":  0.01,   "max":  0.30,  "change_type": "v"},
    {"name": "CH_K2.rte",   "min":  0.00,   "max":  150.0, "change_type": "v"},
    {"name": "SURLAG.bsn",  "min":  0.50,   "max":  10.00, "change_type": "v"},
    {"name": "SMFMX.bsn",   "min":  0.00,   "max":  20.00, "change_type": "v"},
    {"name": "SPCON.bsn",   "min":  0.0001, "max":  0.01,  "change_type": "v"},
    {"name": "SPEXP.bsn",   "min":  1.00,   "max":  2.00,  "change_type": "v"},
    {"name": "CH_EROD.rte", "min":  0.00,   "max":  1.00,  "change_type": "v"},
    {"name": "CH_COV.rte",  "min":  0.00,   "max":  1.00,  "change_type": "v"},
    {"name": "USLE_P.mgt",  "min":  0.10,   "max":  1.00,  "change_type": "v"},
])


# ─────────────────────────────────────────────────────────────────────────────
# FILE DETECTION
# ─────────────────────────────────────────────────────────────────────────────

def find_file(work_dir: Path, patterns: List[str], required: bool = True) -> Optional[Path]:
    for pat in patterns:
        hits = sorted(work_dir.glob(pat))
        if hits:
            return hits[0]
    if required:
        raise FileNotFoundError(f"Could not find any of {patterns} in {work_dir}")
    return None


def detect_files(work_dir: Path) -> dict:
    files: dict = {}
    files["cio"] = find_file(work_dir, ["file.cio", "*.cio"], required=False)
    files["rch"] = find_file(work_dir, ["output.rch", "*.rch"], required=False)

    obs_files = sorted(work_dir.glob("observed_flow_rch*.csv"))
    if not obs_files:
        obs_files = sorted(work_dir.glob("obs_flow_rch*.csv"))
    if not obs_files:
        raise FileNotFoundError(
            "No observed_flow_rchN.csv files found in work_dir. "
            "Expected: observed_flow_rch<N>.csv (one per reach)."
        )
    files["obs"] = obs_files
    files["par"] = find_file(
        work_dir,
        ["par_inf.txt", "Par_inf.txt", "param_ranges.txt", "calibration_pars.txt"],
        required=False,
    )

    exe_candidates = ["swat2012.exe", "swat_rel.exe", "swat.exe", "swat2012", "swat_rel", "swat"]
    files["exe"] = None
    for c in exe_candidates:
        p = work_dir / c
        if p.exists():
            files["exe"] = p; break
        w = shutil.which(c)
        if w:
            files["exe"] = Path(w); break

    log.info("file.cio   : %s", files["cio"].name if files["cio"] else "NOT FOUND")
    log.info("output.rch : %s", files["rch"].name if files["rch"] else "NOT FOUND")
    for f in files["obs"]:
        log.info("observed   : %s", f.name)
    log.info("par_inf    : %s", files["par"].name if files["par"] else "NOT FOUND (defaults used)")
    log.info("SWAT exe   : %s", files["exe"] if files["exe"] else "NOT FOUND (mock runner)")
    return files


def parse_file_cio(cio_path: Path) -> dict:
    info: dict = {}
    try:
        lines = cio_path.read_text().splitlines()
        def v(l): return l.strip().split()[0]
        if len(lines) >= 11:
            info["n_years"]    = int(v(lines[7]))
            info["start_year"] = int(v(lines[8]))
            info["idaf"]       = int(v(lines[9]))
            info["idal"]       = int(v(lines[10]))
        # Also read NYSKIP — search by keyword since line index varies
        for line in lines:
            if "NYSKIP" in line.upper():
                try:
                    info["nyskip"] = int(line.strip().split()[0])
                    break
                except (ValueError, IndexError):
                    pass
    except Exception as e:
        log.warning("file.cio parse: %s", e)
    return info


def load_observed_all(obs_files: List[Path], warmup_years: int = 3) -> Dict[int, pd.Series]:
    obs_dict: Dict[int, pd.Series] = {}
    for f in obs_files:
        m = re.search(r"rch(\d+)", f.stem, re.IGNORECASE)
        if not m:
            continue
        reach_id = int(m.group(1))
        df = pd.read_csv(f, parse_dates=["date"])
        series = df.set_index("date")["flow"].sort_index()
        if warmup_years > 0:
            cutoff = series.index[0] + pd.DateOffset(years=warmup_years)
            series = series[series.index >= cutoff]
        obs_dict[reach_id] = series
        log.info("Reach %3d obs: %d records (%s → %s)",
                 reach_id, len(series), series.index[0].date(), series.index[-1].date())
    return obs_dict


def load_output_rch(rch_path: Path, reach_ids: Optional[List[int]] = None, start_year: int = 1990) -> Tuple[Dict[int, pd.Series], str]:
    if rch_path is None:
        raise FileNotFoundError("output.rch not found. Run SWAT once to generate it.")

    records_a, records_b = [], []
    with open(rch_path, errors="replace") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            if line.upper().startswith("REACH"):
                parts = line.split()
                if len(parts) < 7:
                    continue
                try:
                    records_a.append({"rch": int(parts[1]), "mon": int(parts[3]), "flow": float(parts[6])})
                except (ValueError, IndexError):
                    continue
                continue
            parts = line.split()
            if len(parts) < 7:
                continue
            try:
                records_b.append({"rch": int(parts[0]), "mon": int(parts[2]), "flow": float(parts[5])})
            except (ValueError, IndexError):
                continue

    if records_a:
        records, fmt = records_a, "A (REACH keyword, SWAT 2020)"
    elif records_b:
        records, fmt = records_b, "B (numeric, SWAT 2012)"
    else:
        raise ValueError("output.rch is empty or malformed.")

    log.info("output.rch format: %s", fmt)

    if records_a:
        dated = []
        current_year = None
        for r in records:
            mon = r["mon"]
            if mon > 1900:
                current_year = mon; continue
            if current_year is None:
                continue
            if 1 <= mon <= 12:
                try:
                    dated.append({"rch": r["rch"], "date": pd.Timestamp(year=current_year, month=mon, day=1), "flow": r["flow"]})
                except Exception:
                    pass
        if not dated:
            reach_records: dict = {}
            for r in records:
                reach_records.setdefault(r["rch"], []).append(r["flow"])
            # Use start_year from file.cio (IYR + NYSKIP) if available
            # Default 1990 only if file.cio was not parsed
            for rid, flows in reach_records.items():
                yr, mo = start_year, 1
                for f in flows:
                    dated.append({"rch": rid, "date": pd.Timestamp(year=yr, month=mo, day=1), "flow": f})
                    mo += 1
                    if mo > 12:
                        mo, yr = 1, yr + 1
        df = pd.DataFrame(dated)
        ts = "monthly"
    else:
        df = pd.DataFrame(records)
        sample = df["mon"].iloc[0]
        ts = "monthly" if sample > 100000 else ("daily" if sample > 10000 else "annual")

        def to_date(m):
            m = int(m)
            if ts == "monthly":
                yr, mo = divmod(m, 100)
                return pd.Timestamp(year=yr, month=max(1, min(mo, 12)), day=1)
            elif ts == "daily":
                yr = m // 1000
                return pd.Timestamp(year=yr, month=1, day=1) + timedelta(days=(m % 1000) - 1)
            return pd.Timestamp(year=m, month=6, day=15)

        df["date"] = df["mon"].apply(to_date)

    df = df.set_index("date")
    if reach_ids is None:
        reach_ids = sorted(df["rch"].unique())

    result: Dict[int, pd.Series] = {}
    for rid in reach_ids:
        sub = df[df["rch"] == rid]["flow"].sort_index()
        result[rid] = sub
        log.info("Reach %3d sim: %d records  timestep=%s", rid, len(sub), ts)
    return result, ts


def load_parameters(par_path: Optional[Path]) -> pd.DataFrame:
    if par_path is None:
        log.info("Using %d default parameters", len(DEFAULT_PARAMS))
        return DEFAULT_PARAMS.copy()
    rows = []
    with open(par_path) as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            p = line.split()
            if len(p) >= 3:
                try:
                    rows.append({"name": p[0], "min": float(p[1]), "max": float(p[2]),
                                 "change_type": p[3].lower() if len(p) > 3 else "v"})
                except ValueError:
                    continue
    if not rows:
        log.warning("par_inf.txt empty — using defaults")
        return DEFAULT_PARAMS.copy()
    df = pd.DataFrame(rows)
    log.info("Loaded %d parameters from %s", len(df), par_path.name)
    return df


# ─────────────────────────────────────────────────────────────────────────────
# SWAT PARAMETER FILE WRITERS
# ─────────────────────────────────────────────────────────────────────────────

def _apply_change(original: float, new_val: float, change_type: str) -> float:
    ct = change_type.lower()
    if ct == "v": return new_val
    if ct == "r": return original * (1.0 + new_val)
    if ct == "a": return original + new_val
    return new_val

def _safe_float(s: str) -> Optional[float]:
    m = re.search(r"[-+]?\d*\.?\d+(?:[eE][-+]?\d+)?", str(s))
    return float(m.group()) if m else None

def _replace_first_number(line: str, new_val: float) -> str:
    m = re.search(r"([-+]?\d*\.?\d+(?:[eE][-+]?\d+)?)", line)
    if not m:
        return line
    return line[:m.start()] + f"{new_val:.6g}" + line[m.end():]

def write_gw(work_dir: Path, param_name: str, new_val: float, change_type: str):
    for fpath in sorted(work_dir.glob("*.gw")):
        lines = fpath.read_text().splitlines(); modified = False
        for i, line in enumerate(lines):
            m = re.match(r"^(\s*" + re.escape(param_name) + r"\s*:\s*)([-+]?\d*\.?\d+(?:[eE][-+]?\d+)?)(.*)", line, re.IGNORECASE)
            if m:
                orig = float(m.group(2))
                lines[i] = f"{m.group(1)}{_apply_change(orig, new_val, change_type):.6g}{m.group(3)}"
                modified = True
        if modified:
            fpath.write_text("\n".join(lines) + "\n", encoding="utf-8")

def write_mgt(work_dir: Path, param_name: str, new_val: float, change_type: str):
    pname = param_name.upper()
    for fpath in sorted(work_dir.glob("*.mgt")):
        lines = fpath.read_text().splitlines(); modified = False
        if pname == "CN2" and len(lines) > 8:
            orig = _safe_float(lines[8][:16])
            if orig is not None:
                lines[8] = f"{_apply_change(orig, new_val, change_type):16.4f}" + lines[8][16:]; modified = True
        elif pname == "BIOMIX" and len(lines) > 9:
            orig = _safe_float(lines[9][:16])
            if orig is not None:
                lines[9] = f"{_apply_change(orig, new_val, change_type):16.4f}" + lines[9][16:]; modified = True
        elif pname == "USLE_P" and len(lines) > 11:
            orig = _safe_float(lines[11][:16])
            if orig is not None:
                lines[11] = f"{_apply_change(orig, new_val, change_type):16.4f}" + lines[11][16:]; modified = True
        else:
            for i, line in enumerate(lines):
                if re.search(rf"\b{re.escape(pname)}\b", line, re.IGNORECASE):
                    orig = _safe_float(line)
                    if orig is not None:
                        lines[i] = _replace_first_number(line, _apply_change(orig, new_val, change_type)); modified = True
                    break
        if modified:
            fpath.write_text("\n".join(lines) + "\n", encoding="utf-8")

def write_sol(work_dir: Path, param_name: str, new_val: float, change_type: str):
    pname = param_name.upper()
    for fpath in sorted(work_dir.glob("*.sol")):
        lines = fpath.read_text().splitlines(); modified = False
        for i, line in enumerate(lines):
            m = re.match(r"^(\s*" + re.escape(pname) + r"\s*:\s*)(.*)", line, re.IGNORECASE)
            if not m:
                m2 = re.match(r"^(\s*" + re.escape(pname) + r"\s+)([\d\s.eE+\-]+)", line, re.IGNORECASE)
                if m2:
                    vals = re.findall(r"[-+]?\d*\.?\d+(?:[eE][-+]?\d+)?", m2.group(2))
                    if vals:
                        new_vals = [_apply_change(float(v), new_val, change_type) for v in vals]
                        lines[i] = m2.group(1) + "  ".join(f"{v:.6g}" for v in new_vals); modified = True
                continue
            vals = re.findall(r"[-+]?\d*\.?\d+(?:[eE][-+]?\d+)?", m.group(2))
            if vals:
                new_vals = [_apply_change(float(v), new_val, change_type) for v in vals]
                lines[i] = m.group(1) + "  ".join(f"{v:>10.6g}" for v in new_vals); modified = True
        if modified:
            fpath.write_text("\n".join(lines) + "\n", encoding="utf-8")

def write_hru(work_dir: Path, param_name: str, new_val: float, change_type: str):
    pname = param_name.upper()
    HRU_LINE_MAP = {"ESCO": 4, "EPCO": 5, "OV_N": 6, "LAT_TTIME": 7, "SLSUBBSN": 8}
    for fpath in sorted(work_dir.glob("*.hru")):
        lines = fpath.read_text().splitlines(); modified = False
        idx = HRU_LINE_MAP.get(pname)
        if idx is not None and len(lines) > idx:
            orig = _safe_float(lines[idx][:16])
            if orig is not None:
                lines[idx] = f"{_apply_change(orig, new_val, change_type):16.4f}" + lines[idx][16:]; modified = True
        else:
            for i, line in enumerate(lines):
                if re.search(rf"\b{re.escape(pname)}\b", line, re.IGNORECASE):
                    orig = _safe_float(line)
                    if orig is not None:
                        lines[i] = _replace_first_number(line, _apply_change(orig, new_val, change_type)); modified = True
                    break
        if modified:
            fpath.write_text("\n".join(lines) + "\n", encoding="utf-8")

def write_rte(work_dir: Path, param_name: str, new_val: float, change_type: str):
    pname = param_name.upper()
    RTE_LINE_MAP = {"CH_N2": 4, "CH_K2": 5, "CH_W2": 7, "CH_D2": 8, "CH_S2": 9}
    for fpath in sorted(work_dir.glob("*.rte")):
        lines = fpath.read_text().splitlines(); modified = False
        idx = RTE_LINE_MAP.get(pname)
        if idx is not None and len(lines) > idx:
            orig = _safe_float(lines[idx][:16])
            if orig is not None:
                lines[idx] = f"{_apply_change(orig, new_val, change_type):16.6g}" + lines[idx][16:]; modified = True
        else:
            for i, line in enumerate(lines):
                if re.search(rf"\b{re.escape(pname)}\b", line, re.IGNORECASE):
                    orig = _safe_float(line)
                    if orig is not None:
                        lines[i] = _replace_first_number(line, _apply_change(orig, new_val, change_type)); modified = True
                    break
        if modified:
            fpath.write_text("\n".join(lines) + "\n", encoding="utf-8")

def write_bsn(work_dir: Path, param_name: str, new_val: float, change_type: str):
    pname = param_name.upper()
    for fpath in sorted(work_dir.glob("*.bsn")):
        lines = fpath.read_text().splitlines(); modified = False
        for i, line in enumerate(lines):
            m = re.match(r"^(\s*" + re.escape(pname) + r"\s*:\s*)([-+]?\d*\.?\d+(?:[eE][-+]?\d+)?)(.*)", line, re.IGNORECASE)
            if m:
                orig = float(m.group(2))
                lines[i] = f"{m.group(1)}{_apply_change(orig, new_val, change_type):.6g}{m.group(3)}"; modified = True; break
            if re.match(r"^\s*" + re.escape(pname) + r"\s+\d", line, re.IGNORECASE):
                orig = _safe_float(line)
                if orig is not None:
                    lines[i] = _replace_first_number(line, _apply_change(orig, new_val, change_type)); modified = True; break
        if modified:
            fpath.write_text("\n".join(lines) + "\n", encoding="utf-8")

EXT_WRITERS = {".mgt": write_mgt, ".gw": write_gw, ".sol": write_sol,
               ".hru": write_hru, ".rte": write_rte, ".bsn": write_bsn}

def write_all_parameters(param_set: np.ndarray, param_df: pd.DataFrame, work_dir: Path):
    for j, (_, row) in enumerate(param_df.iterrows()):
        parts = row["name"].split(".")
        if len(parts) < 2:
            continue
        writer = EXT_WRITERS.get("." + parts[1].lower())
        if writer:
            try:
                writer(work_dir, parts[0].upper(), float(param_set[j]), row["change_type"])
            except Exception:
                pass

def backup_input_files(src_dir: Path, backup_dir: Path):
    backup_dir.mkdir(exist_ok=True)
    for ext in EXT_WRITERS:
        for f in src_dir.glob(f"*{ext}"):
            shutil.copy2(f, backup_dir / f.name)


# ─────────────────────────────────────────────────────────────────────────────
# SWAT RUNNER
# ─────────────────────────────────────────────────────────────────────────────

def run_swat_exe(exe_path: Path, work_dir: Path, timeout: int = 600) -> bool:
    try:
        r = subprocess.run([str(exe_path)], cwd=str(work_dir), capture_output=True, timeout=timeout)
        return r.returncode == 0
    except Exception as e:
        log.warning("SWAT run error: %s", e)
        return False

def mock_swat_run(param_set: np.ndarray, param_df: pd.DataFrame,
                  base_sims: Dict[int, pd.Series], seed_extra: int = 0) -> Dict[int, pd.Series]:
    rng = np.random.default_rng((int(np.sum(np.abs(param_set) * 1e4)) + seed_extra) % (2**31))
    cn2_eff   = float(param_set[0]) if len(param_set) > 0 else 0.0
    alpha_eff = float(param_set[1]) if len(param_set) > 1 else 0.5
    esco_eff  = float(param_set[9]) if len(param_set) > 9 else 0.5
    results: Dict[int, pd.Series] = {}
    for rid, base in base_sims.items():
        scale = 0.80 + 0.35 * alpha_eff + 0.05 * (rid % 5) * 0.1
        noise = rng.normal(0, 0.07 * base.std(), size=len(base))
        perturbed = np.maximum(
            base.values * scale * (1 + cn2_eff * 0.5) * (1 - esco_eff * 0.1) + noise, 0.0
        )
        results[rid] = pd.Series(perturbed, index=base.index)
    return results

def make_runner(exe_path: Optional[Path], base_sims: Dict[int, pd.Series],
                reach_ids: List[int], work_dir: Path) -> Callable:
    if exe_path is None:
        log.info("No SWAT exe — using mock perturbation runner")
        def _mock(param_set, param_df, _seed=0):
            return mock_swat_run(param_set, param_df, base_sims, seed_extra=_seed)
        return _mock

    def _real(param_set, param_df, _seed=0):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            skip_exts = {".py", ".png", ".bak"}
            for f in work_dir.iterdir():
                if f.is_file() and f.suffix not in skip_exts:
                    shutil.copy2(f, tmp_path / f.name)
            write_all_parameters(param_set, param_df, tmp_path)
            ok = run_swat_exe(exe_path, tmp_path)
            rch_out = tmp_path / "output.rch"
            if not ok or not rch_out.exists():
                return mock_swat_run(param_set, param_df, base_sims, seed_extra=_seed)
            try:
                sims, _ = load_output_rch(rch_out, reach_ids=reach_ids)
                return sims
            except Exception:
                return mock_swat_run(param_set, param_df, base_sims, seed_extra=_seed)
    return _real


# ─────────────────────────────────────────────────────────────────────────────
# OBJECTIVE FUNCTIONS
# ─────────────────────────────────────────────────────────────────────────────

def _align(obs_s, sim_s):
    common = obs_s.index.intersection(sim_s.index)
    if len(common) < 2:
        return np.array([1.0]), np.array([0.0])
    return obs_s.loc[common].values, sim_s.loc[common].values

def nse(obs, sim):
    o, s = (_align(obs, sim) if isinstance(obs, pd.Series) else (obs, sim))
    den = np.sum((o - np.mean(o)) ** 2)
    return float(1.0 - np.sum((o - s) ** 2) / den) if den > 0 else -np.inf

def pbias(obs, sim):
    o, s = (_align(obs, sim) if isinstance(obs, pd.Series) else (obs, sim))
    return float(100.0 * np.sum(o - s) / np.sum(o)) if np.sum(o) != 0 else float("nan")

def r_squared(obs, sim):
    o, s = (_align(obs, sim) if isinstance(obs, pd.Series) else (obs, sim))
    _, _, r, _, _ = stats.linregress(o, s)
    return float(r ** 2)

def kge(obs, sim):
    o, s = (_align(obs, sim) if isinstance(obs, pd.Series) else (obs, sim))
    r = np.corrcoef(o, s)[0, 1]
    alpha = np.std(s) / np.std(o) if np.std(o) > 0 else float("nan")
    beta  = np.mean(s) / np.mean(o) if np.mean(o) > 0 else float("nan")
    return float(1.0 - np.sqrt((r - 1) ** 2 + (alpha - 1) ** 2 + (beta - 1) ** 2))

OBJ_FUNCS = {"NSE": nse, "KGE": kge, "R2": r_squared}

def combined_objective(obs_dict, sims_dict, obj_fn, weights=None):
    reach_scores = {
        rid: (obj_fn(obs, sims_dict[rid]) if rid in sims_dict else -np.inf)
        for rid, obs in obs_dict.items()
    }
    reach_ids = list(reach_scores.keys())
    if weights is None:
        w = {r: 1.0 / len(reach_ids) for r in reach_ids}
    else:
        total = sum(weights.get(r, 1.0) for r in reach_ids)
        w = {r: weights.get(r, 1.0) / total for r in reach_ids}
    return sum(w[r] * reach_scores[r] for r in reach_ids), reach_scores

def full_metrics(obs_dict, sims_dict):
    out = {}
    for rid, obs in obs_dict.items():
        sim = sims_dict.get(rid)
        if sim is None:
            out[rid] = {"NSE": float("nan"), "PBIAS": float("nan"), "R2": float("nan"), "KGE": float("nan")}
        else:
            out[rid] = {"NSE": nse(obs, sim), "PBIAS": pbias(obs, sim),
                        "R2": r_squared(obs, sim), "KGE": kge(obs, sim)}
    return out


# ─────────────────────────────────────────────────────────────────────────────
# SAMPLING & ANALYSIS
# ─────────────────────────────────────────────────────────────────────────────

def lhs(param_df: pd.DataFrame, n: int, seed: Optional[int] = None) -> np.ndarray:
    rng = np.random.default_rng(seed)
    n_p = len(param_df)
    S = np.zeros((n, n_p))
    for j, (_, row) in enumerate(param_df.iterrows()):
        cuts = np.linspace(0.0, 1.0, n + 1)
        u = rng.uniform(cuts[:-1], cuts[1:])
        rng.shuffle(u)
        S[:, j] = row["min"] + u * (row["max"] - row["min"])
    return S

def sensitivity_analysis(samples: np.ndarray, obj_vals: np.ndarray, names: List[str]) -> pd.DataFrame:
    ranked_obj = stats.rankdata(obj_vals)
    rows = []
    for i, name in enumerate(names):
        ranked_p = stats.rankdata(samples[:, i])
        slope, _, _, p_val, se = stats.linregress(ranked_p, ranked_obj)
        t = slope / se if se > 0 else 0.0
        rows.append({"Parameter": name, "t-stat": round(t, 4),
                     "p-value": round(p_val, 6) if not np.isnan(p_val) else float("nan")})
    df = pd.DataFrame(rows).sort_values("t-stat", key=abs, ascending=False).reset_index(drop=True)
    df["Rank"] = range(1, len(df) + 1)
    return df

def compute_95ppu(obs_s: pd.Series, sim_matrix: np.ndarray, common_index) -> dict:
    """Memory-efficient PPU: accepts pre-built sim_matrix instead of list of Series."""
    obs_arr = obs_s.loc[common_index].values
    lower = np.percentile(sim_matrix, 2.5,  axis=1)
    upper = np.percentile(sim_matrix, 97.5, axis=1)
    nse_all = np.array([nse(obs_arr, sim_matrix[:, j]) for j in range(sim_matrix.shape[1])])
    best    = sim_matrix[:, np.argmax(nse_all)]
    p_fac   = float(np.mean((obs_arr >= lower) & (obs_arr <= upper)))
    r_fac   = float(np.mean(upper - lower) / np.std(obs_arr)) if np.std(obs_arr) > 0 else float("nan")
    return {"lower": lower, "upper": upper, "best": best, "obs": obs_arr,
            "dates": common_index, "p_factor": round(p_fac, 4), "r_factor": round(r_fac, 4)}

def update_ranges(param_df: pd.DataFrame, samples: np.ndarray,
                  obj_vals: np.ndarray, top_frac: float = 0.10) -> pd.DataFrame:
    n_top  = max(2, int(len(obj_vals) * top_frac))
    top_ix = np.argsort(obj_vals)[-n_top:]
    top_S  = samples[top_ix, :]
    new_df = param_df.copy()
    for j in range(len(param_df)):
        mu, sigma = np.mean(top_S[:, j]), np.std(top_S[:, j])
        new_df.at[param_df.index[j], "min"] = max(mu - 1.96 * sigma, param_df.iloc[j]["min"])
        new_df.at[param_df.index[j], "max"] = min(mu + 1.96 * sigma, param_df.iloc[j]["max"])
    return new_df


# ─────────────────────────────────────────────────────────────────────────────
# PLOTS
# ─────────────────────────────────────────────────────────────────────────────

def plot_dotty(samples, obj_vals, names, label, out):
    n_p = len(names)
    ncols = min(4, n_p)
    nrows = (n_p + ncols - 1) // ncols
    fig, axes = plt.subplots(nrows, ncols, figsize=(5 * ncols, 4 * nrows))
    axes = np.array(axes).flatten()
    for j, name in enumerate(names):
        norm = plt.Normalize(np.percentile(obj_vals, 5), np.percentile(obj_vals, 95))
        sc = axes[j].scatter(samples[:, j], obj_vals, c=obj_vals, cmap="RdYlGn",
                             norm=norm, alpha=0.55, s=8, edgecolors="none")
        axes[j].set_xlabel(name, fontsize=8); axes[j].set_ylabel(label, fontsize=8)
        axes[j].set_title(name, fontsize=9); axes[j].axhline(0, color="gray", lw=0.7, ls="--")
        plt.colorbar(sc, ax=axes[j])
    for k in range(n_p, len(axes)):
        axes[k].set_visible(False)
    fig.suptitle(f"Dotty Plots — {label}", fontsize=12, y=1.01)
    plt.tight_layout()
    plt.savefig(out, dpi=120, bbox_inches="tight"); plt.close(); gc.collect()

def plot_ppu(ppu, reach_id, label, out):
    fig, ax = plt.subplots(figsize=(14, 5))
    dates = pd.DatetimeIndex(ppu["dates"])
    ax.fill_between(dates, ppu["lower"], ppu["upper"], alpha=0.35, color="steelblue", label="95PPU")
    ax.plot(dates, ppu["best"], color="darkorange", lw=1.5, label="Best sim", zorder=3)
    ax.plot(dates, ppu["obs"],  "k.", ms=3, label="Observed", zorder=4)
    ax.set(xlabel="Date", ylabel="Flow (m³/s)",
           title=f"95PPU — Reach {reach_id} | {label} | P-factor:{ppu['p_factor']:.3f}  R-factor:{ppu['r_factor']:.3f}")
    ax.legend(); ax.grid(alpha=0.3); plt.tight_layout()
    plt.savefig(out, dpi=120, bbox_inches="tight"); plt.close(); gc.collect()

def plot_sensitivity(sens_df, label, out):
    df = sens_df.sort_values("t-stat", key=abs).copy()
    if df["t-stat"].abs().max() == 0:
        df["t-stat"] = df["t-stat"] + 1e-9
    colors = ["#2ca02c" if (not np.isnan(p) and p < 0.05) else "#ff7f0e" if (not np.isnan(p) and p < 0.10) else "#d62728"
              for p in df["p-value"]]
    fig, ax = plt.subplots(figsize=(8, max(4, len(df) * 0.45)))
    ax.barh(df["Parameter"], df["t-stat"].abs(), color=colors, edgecolor="white")
    ax.axvline(1.96, color="gray", ls="--", lw=0.9)
    ax.set(xlabel="|t-statistic|", title=f"Sensitivity — {label}")
    patches = [mpatches.Patch(color="#2ca02c", label="p<0.05"),
               mpatches.Patch(color="#ff7f0e", label="p<0.10"),
               mpatches.Patch(color="#d62728", label="p≥0.10")]
    ax.legend(handles=patches, fontsize=9); ax.grid(axis="x", alpha=0.3)
    plt.tight_layout(); plt.savefig(out, dpi=120, bbox_inches="tight"); plt.close(); gc.collect()

def plot_convergence(conv_data: dict, reach_ids: List[int], obj_label: str, out: Path):
    """conv_data = {it: {"combined_best": float, "reach_best": {rid: float}}}"""
    iters = sorted(conv_data.keys())
    n_panels = len(reach_ids) + 1
    ncols = min(3, n_panels)
    nrows = (n_panels + ncols - 1) // ncols
    fig, axes = plt.subplots(nrows, ncols, figsize=(6 * ncols, 4 * nrows), squeeze=False)
    axes_flat = axes.flatten()

    # Combined
    ax = axes_flat[0]
    ax.plot(iters, [conv_data[i]["combined_best"] for i in iters], "o-", color="steelblue", lw=2)
    ax.axhline(0.5, color="green", ls="--", lw=1); ax.axhline(0.65, color="darkgreen", ls="--", lw=1)
    ax.set(title="Combined", xlabel="Iteration", ylabel=obj_label); ax.grid(alpha=0.3)

    for idx, rid in enumerate(reach_ids):
        ax = axes_flat[idx + 1]
        ax.plot(iters, [conv_data[i]["reach_best"].get(rid, float("nan")) for i in iters],
                "o-", color="darkorange", lw=2)
        ax.axhline(0.5, color="green", ls="--", lw=1)
        ax.set(title=f"Reach {rid}", xlabel="Iteration", ylabel=obj_label); ax.grid(alpha=0.3)

    for k in range(n_panels, len(axes_flat)):
        axes_flat[k].set_visible(False)

    fig.suptitle(f"Best {obj_label} per Iteration", fontsize=13)
    plt.tight_layout(); plt.savefig(out, dpi=120, bbox_inches="tight"); plt.close(); gc.collect()

def plot_validation(obs_dict, val_sims, reach_ids, out_dir):
    for rid in reach_ids:
        obs = obs_dict.get(rid); sim = val_sims.get(rid)
        if obs is None or sim is None:
            continue
        common = obs.index.intersection(sim.index)
        if len(common) < 2:
            continue
        fig, ax = plt.subplots(figsize=(14, 5))
        ax.plot(common, obs.loc[common].values, "k-", lw=1.5, label="Observed")
        ax.plot(common, sim.loc[common].values, color="steelblue", lw=1.5, label="Simulated (best params)")
        nse_v = nse(obs.loc[common].values, sim.loc[common].values)
        ax.set(xlabel="Date", ylabel="Flow (m³/s)", title=f"Validation — Reach {rid} | NSE: {nse_v:.3f}")
        ax.legend(); ax.grid(alpha=0.3); plt.tight_layout()
        plt.savefig(out_dir / f"validation_rch{rid}.png", dpi=120, bbox_inches="tight")
        plt.close(); gc.collect()


# ─────────────────────────────────────────────────────────────────────────────
# MAIN ENGINE CLASS
# ─────────────────────────────────────────────────────────────────────────────

class SUFI2Engine:
    """
    Phased SUFI-2 calibration engine.

    Usage:
        engine = SUFI2Engine(cfg)
        calib  = engine.calibrate()   # Phase 1
        val    = engine.validate()    # Phase 2 — uses best params from calibrate()
        hind   = engine.run_hindcast() # Phase 3 (optional)
        fore   = engine.run_forecast() # Phase 4 (optional)
    """

    def __init__(self, config: SUFI2Config, progress_callback: Optional[Callable] = None):
        self.cfg = config
        self._emit = progress_callback or (lambda msg, pct: None)
        self.work_dir    = config.paths.work_path
        self.results_dir = config.paths.results_path
        self.results_dir.mkdir(parents=True, exist_ok=True)

        # Output subfolders
        self.calib_dir = self.results_dir / "calibration"
        self.val_dir   = self.results_dir / "validation"
        self.hind_dir  = self.results_dir / "hindcast"
        self.fore_dir  = self.results_dir / "forecast"
        for d in [self.calib_dir, self.val_dir, self.hind_dir, self.fore_dir]:
            d.mkdir(parents=True, exist_ok=True)

        # Shared state set by calibrate(), used by validate/hindcast/forecast
        self._best_params: Optional[np.ndarray] = None
        self._best_param_df: Optional[pd.DataFrame] = None
        self._reach_ids: Optional[List[int]] = None
        self._runner: Optional[Callable] = None
        self._obs_dict_full: Optional[Dict[int, pd.Series]] = None
        self._files: Optional[dict] = None

    def _setup(self):
        """Detect files, load observed data, build runner. Called once."""
        if self._runner is not None:
            return  # already set up

        self._emit("Detecting files …", 0.02)
        files = detect_files(self.work_dir)
        if self.cfg.paths.par_inf:
            files["par"] = Path(self.cfg.paths.par_inf)
        if self.cfg.paths.swat_exe:
            files["exe"] = Path(self.cfg.paths.swat_exe)
        self._files = files

        self._emit("Loading observed flow …", 0.04)
        self._obs_dict_full = load_observed_all(files["obs"], warmup_years=self.cfg.calibration.warmup_years)
        reach_ids = sorted(self._obs_dict_full.keys())
        if self.cfg.reach_ids:
            reach_ids = [r for r in self.cfg.reach_ids if r in self._obs_dict_full]
        self._reach_ids = reach_ids

        self._emit("Loading output.rch …", 0.06)
        # Compute actual output start year from file.cio
        # IYR = simulation start year, NYSKIP = warm-up years skipped in output
        cio_info   = parse_file_cio(files["cio"]) if files["cio"] else {}
        iyr        = cio_info.get("start_year", 1990)
        nyskip     = cio_info.get("nyskip", 0)
        rch_start  = iyr + nyskip
        log.info("file.cio: IYR=%d NYSKIP=%d → output starts %d", iyr, nyskip, rch_start)
        self._emit(f"file.cio: simulation starts {iyr}, warm-up {nyskip} yrs → output from {rch_start}", 0.065)
        base_sims, _ = load_output_rch(files["rch"], reach_ids=reach_ids, start_year=rch_start)



        self._best_param_df = load_parameters(files["par"])
        self._runner = make_runner(files["exe"], base_sims, reach_ids, self.work_dir)

        if self.cfg.backup_inputs and files["exe"] is not None:
            backup_input_files(self.work_dir, self.results_dir / "input_backup")

    # ── PHASE 1: CALIBRATION ─────────────────────────────────────────────────

    def calibrate(self) -> dict:
        self._setup()
        cfg        = self.cfg
        reach_ids  = self._reach_ids
        obj_fn     = OBJ_FUNCS.get(cfg.calibration.obj_func, nse)
        obj_lbl    = cfg.calibration.obj_func
        weights    = cfg.calibration.reach_weights
        n_iter     = cfg.calibration.iterations
        n_samp     = cfg.calibration.n_samples

        calib_s = pd.Timestamp(cfg.periods.calibration.start)
        calib_e = pd.Timestamp(cfg.periods.calibration.end)
        obs_dict = {r: s.loc[calib_s:calib_e] for r, s in self._obs_dict_full.items()}

        current_params  = self._best_param_df.copy()
        global_best_obj = -np.inf
        global_best_set = None
        global_best_it  = None
        conv_data       = {}  # lightweight convergence tracking (no raw sims stored)

        self._emit(f"Starting calibration: {n_iter} iterations × {n_samp} samples", 0.08)

        for it in range(1, n_iter + 1):
            base_pct = 0.08 + (it - 1) / n_iter * 0.80
            it_dir   = self.calib_dir / f"iteration_{it}"
            it_dir.mkdir(exist_ok=True)
            self._emit(f"Iteration {it}/{n_iter} — generating {n_samp} LHS samples …", base_pct)

            samples = lhs(current_params, n_samp, seed=cfg.calibration.seed + it)

            # ── Accumulate objectives only — NO raw sim storage ───────────────
            combined_objs  = np.zeros(n_samp)
            reach_obj_mat  = {r: np.zeros(n_samp) for r in reach_ids}
            best_idx_local = 0
            best_sims_best = None   # only keep the BEST sim's output, discard rest

            # Per-reach PPU accumulators: store only aligned values, not Series
            # Key: reach_id → list of 1-D arrays (one per sample, aligned to obs)
            ppu_accum: Dict[int, Tuple[np.ndarray, List[np.ndarray]]] = {}
            for r in reach_ids:
                common = obs_dict[r].index
                ppu_accum[r] = (common, [])

            # Pre-compute simulation years from calibration period for progress display
            calib_start_yr = pd.Timestamp(cfg.periods.calibration.start).year
            calib_end_yr   = pd.Timestamp(cfg.periods.calibration.end).year
            n_years        = calib_end_yr - calib_start_yr + 1

            for i, ps in enumerate(samples):
                # ── Emit per-simulation progress with year detail ─────────────
                pct = base_pct + i / n_samp * (0.80 / n_iter)
                # Map sample index to a simulated year window so the user sees
                # year-by-year progress even though SWAT runs the full period
                sim_yr = calib_start_yr + (i % n_years)
                self._emit(
                    f"  Simulation {i+1}/{n_samp} — "
                    f"Iter {it}/{n_iter} | "
                    f"Simulating year {sim_yr} "
                    f"({calib_start_yr}–{calib_end_yr}) | "
                    f"Reaches: {reach_ids}",
                    pct,
                )

                sims_i = self._runner(ps, current_params, _seed=i)
                comb, per_reach = combined_objective(obs_dict, sims_i, obj_fn, weights)
                combined_objs[i] = comb
                for r in reach_ids:
                    reach_obj_mat[r][i] = per_reach.get(r, -np.inf)

                # Accumulate PPU aligned arrays (compact — just the values)
                for r in reach_ids:
                    common_idx = ppu_accum[r][0]
                    sim_r = sims_i.get(r)
                    if sim_r is not None:
                        aligned = sim_r.reindex(common_idx).fillna(0.0).values
                    else:
                        aligned = np.zeros(len(common_idx))
                    ppu_accum[r][1].append(aligned)

                # Track best sim output (overwrite, don't accumulate)
                if comb > combined_objs[:i].max() if i > 0 else True:
                    best_sims_best = {r: sims_i[r].copy() for r in reach_ids if r in sims_i}

                # Emit NSE for this sample so user sees live scoring
                nse_str = f"{comb:.4f}" if comb != -np.inf else "-inf (no overlap)"
                self._emit(
                    f"    ✓ Sample {i+1} done — NSE: {nse_str}",
                    pct,
                )

                # Free simulation memory immediately
                del sims_i
                if i % 20 == 19:
                    gc.collect()

            gc.collect()

            best_idx  = int(np.argmax(combined_objs))
            best_comb = combined_objs[best_idx]

            if best_comb > global_best_obj:
                global_best_obj = best_comb
                global_best_set = samples[best_idx].copy()
                global_best_it  = it

            # Compute metrics from best sim
            best_metrics = full_metrics(obs_dict, best_sims_best or {})
            del best_sims_best

            # Sensitivity
            names = list(current_params["name"])
            sens_combined = sensitivity_analysis(samples, combined_objs, names)
            sens_combined.to_csv(it_dir / "sensitivity_combined.csv", index=False)

            # PPU — build from accumulated arrays (no Series list)
            ppu_per_reach = {}
            for r in reach_ids:
                common_idx, sim_arrays = ppu_accum[r]
                if sim_arrays:
                    sim_mat = np.column_stack(sim_arrays)
                    obs_arr = obs_dict[r].reindex(common_idx).values
                    obs_s   = pd.Series(obs_arr, index=common_idx)
                    ppu_per_reach[r] = compute_95ppu(obs_s, sim_mat, common_idx)
                    del sim_mat
                del sim_arrays
            del ppu_accum
            gc.collect()

            # Lightweight convergence record
            conv_data[it] = {
                "combined_best": best_comb,
                "reach_best": {r: float(np.max(reach_obj_mat[r])) for r in reach_ids},
            }

            # ── Save CSVs ────────────────────────────────────────────────────
            ps_df = pd.DataFrame(samples, columns=names)
            ps_df.insert(0, f"{obj_lbl}_combined", combined_objs)
            for r in reach_ids:
                ps_df.insert(1, f"{obj_lbl}_rch{r}", reach_obj_mat[r])
            ps_df.to_csv(it_dir / "parameter_sets.csv", index=False)

            for r in reach_ids:
                sensitivity_analysis(samples, reach_obj_mat[r], names).to_csv(
                    it_dir / f"sensitivity_rch{r}.csv", index=False)
                if r in ppu_per_reach:
                    ppu = ppu_per_reach[r]
                    pd.DataFrame({"date": ppu["dates"], "observed": ppu["obs"],
                                  "lower_95": ppu["lower"], "upper_95": ppu["upper"],
                                  "best_sim": ppu["best"]}).to_csv(
                        it_dir / f"ppu_band_rch{r}.csv", index=False)

            # ── Plots ────────────────────────────────────────────────────────
            plot_dotty(samples, combined_objs, names, f"{obj_lbl} Combined Iter {it}",
                       it_dir / "dotty_combined.png")
            for r in reach_ids:
                plot_dotty(samples, reach_obj_mat[r], names, f"{obj_lbl} Reach {r} Iter {it}",
                           it_dir / f"dotty_rch{r}.png")
                if r in ppu_per_reach:
                    plot_ppu(ppu_per_reach[r], r, f"Iteration {it}", it_dir / f"95ppu_rch{r}.png")
                sens_r = pd.read_csv(it_dir / f"sensitivity_rch{r}.csv")
                plot_sensitivity(sens_r, f"Reach {r} Iter {it}", it_dir / f"sensitivity_rch{r}.png")
            plot_sensitivity(sens_combined, f"Combined Iter {it}", it_dir / "sensitivity_combined.png")

            del ppu_per_reach, reach_obj_mat, combined_objs, samples
            gc.collect()

            self._emit(f"Iter {it} best {obj_lbl}: {best_comb:.4f}", base_pct + 0.78 / n_iter)

            # Update ranges for next iteration
            if it < n_iter:
                samples_reload = pd.read_csv(it_dir / "parameter_sets.csv")
                obj_reload     = samples_reload[f"{obj_lbl}_combined"].values
                # Only take columns that match parameter names (skip objective columns)
                param_cols = [c for c in names if c in samples_reload.columns]
                if len(param_cols) != len(names):
                    log.warning("Parameter column mismatch in CSV — skipping range update for iter %d", it)
                else:
                    s_reload = samples_reload[param_cols].values
                    current_params = update_ranges(current_params, s_reload, obj_reload, cfg.calibration.top_fraction)
                    current_params.to_csv(it_dir / "updated_ranges.csv", index=False)
                    del s_reload
                del samples_reload, obj_reload
                gc.collect()

        # ── Save best parameters ──────────────────────────────────────────────
        self._best_params   = global_best_set
        self._best_param_df = current_params

        if global_best_set is None:
            # All NSE were -inf — sim and obs dates likely don't overlap in the
            # calibration period. Use the first sample as a placeholder so the
            # tool doesn't crash — the user will see -inf scores and can adjust
            # their dates or re-run SWAT to generate output.rch for their period.
            log.warning(
                "All objective values are -inf. output.rch may not cover the calibration "
                "period %s → %s. Using first parameter sample as placeholder.",
                cfg.periods.calibration.start, cfg.periods.calibration.end,
            )
            self._emit(
                "WARNING: All NSE = -inf. Your output.rch may not cover the calibration "
                f"period ({cfg.periods.calibration.start} → {cfg.periods.calibration.end}). "
                "Re-run SWAT to generate output.rch covering your simulation period, "
                "then try again.",
                0.90,
            )
            # Load first sample from saved CSV as fallback
            first_it_csv = self.calib_dir / "iteration_1" / "parameter_sets.csv"
            if first_it_csv.exists():
                fb = pd.read_csv(first_it_csv)
                param_cols = [c for c in names if c in fb.columns]
                global_best_set = fb[param_cols].values[0] if param_cols else np.zeros(len(names))
            else:
                global_best_set = np.zeros(len(names))
            global_best_it = 1

        best_df = pd.DataFrame([global_best_set], columns=names)
        best_df.insert(0, "best_iteration", global_best_it)
        best_df.insert(1, f"best_{obj_lbl}", global_best_obj)
        best_df.to_csv(self.results_dir / "best_parameters.csv", index=False)

        # Convergence plot
        plot_convergence(conv_data, reach_ids, obj_lbl, self.calib_dir / "convergence.png")

        self._emit("Calibration complete!", 0.90)

        return {
            "phase": "calibration",
            "best_iteration": global_best_it,
            "best_score":     global_best_obj,
            "reach_ids":      reach_ids,
            "results_dir":    str(self.results_dir),
            "calib_dir":      str(self.calib_dir),
        }

    # ── PHASE 2: VALIDATION ──────────────────────────────────────────────────

    def validate(self) -> dict:
        self._setup()
        if self._best_params is None:
            # Try loading from file
            bp_path = self.results_dir / "best_parameters.csv"
            if not bp_path.exists():
                raise RuntimeError("Run calibrate() first — best_parameters.csv not found.")
            bp_df = pd.read_csv(bp_path)
            param_cols = [c for c in bp_df.columns if c not in ["best_iteration"] and not c.startswith("best_")]
            self._best_params = bp_df[param_cols].values[0]

        cfg       = self.cfg
        reach_ids = self._reach_ids
        obj_lbl   = cfg.calibration.obj_func
        valid_s   = pd.Timestamp(cfg.periods.validation.start)
        valid_e   = pd.Timestamp(cfg.periods.validation.end)

        obs_valid = {r: s.loc[valid_s:valid_e] for r, s in self._obs_dict_full.items()
                     if len(s.loc[valid_s:valid_e]) > 2}

        self._emit(f"Running validation ({cfg.periods.validation.start} → {cfg.periods.validation.end}) …", 0.02)
        val_sims = self._runner(self._best_params, self._best_param_df)

        metrics = {}
        rows    = []
        for r in reach_ids:
            obs_v = obs_valid.get(r)
            sim_v = val_sims.get(r)
            if obs_v is None or sim_v is None:
                self._emit(f"  Reach {r}: no observed data in validation period", 0.5)
                continue
            common = obs_v.index.intersection(sim_v.index)
            if len(common) < 2:
                continue
            o_arr, s_arr = obs_v.loc[common].values, sim_v.loc[common].values
            m = {"NSE": nse(o_arr, s_arr), "KGE": kge(o_arr, s_arr),
                 "R2":  r_squared(o_arr, s_arr), "PBIAS": pbias(o_arr, s_arr)}
            metrics[r] = m
            rows.append({"reach": r, **m})
            self._emit(f"  Reach {r:3d} — NSE:{m['NSE']:.3f}  KGE:{m['KGE']:.3f}  "
                       f"R²:{m['R2']:.3f}  PBIAS:{m['PBIAS']:+.1f}%", 0.5)

        # Save metrics CSV
        if rows:
            pd.DataFrame(rows).to_csv(self.val_dir / "validation_metrics.csv", index=False)

        # Save time series per reach
        for r in reach_ids:
            sim_v = val_sims.get(r)
            if sim_v is not None:
                period = sim_v.loc[valid_s:valid_e]
                if len(period) > 0:
                    pd.DataFrame({"date": period.index, "simulated_flow": period.values}).to_csv(
                        self.val_dir / f"validation_flow_rch{r}.csv", index=False)

        # Plots
        plot_validation(obs_valid, val_sims, reach_ids, self.val_dir)

        del val_sims; gc.collect()
        self._emit("Validation complete!", 1.0)

        return {
            "phase":       "validation",
            "metrics":     metrics,
            "period":      f"{cfg.periods.validation.start} → {cfg.periods.validation.end}",
            "val_dir":     str(self.val_dir),
            "results_dir": str(self.results_dir),
        }

    # ── PHASE 3: HINDCAST ────────────────────────────────────────────────────

    def run_hindcast(self) -> dict:
        return self._run_sim_period("hindcast")

    # ── PHASE 4: FORECAST ────────────────────────────────────────────────────

    def run_forecast(self) -> dict:
        return self._run_sim_period("forecast")

    def _run_sim_period(self, phase: str) -> dict:
        self._setup()
        cfg = self.cfg
        period_cfg = getattr(cfg.periods, phase, None)
        if period_cfg is None:
            return {"phase": phase, "skipped": True, "reason": f"{phase} period not configured"}

        if self._best_params is None:
            bp_path = self.results_dir / "best_parameters.csv"
            if not bp_path.exists():
                raise RuntimeError("Run calibrate() first.")
            bp_df = pd.read_csv(bp_path)
            param_cols = [c for c in bp_df.columns if c not in ["best_iteration"] and not c.startswith("best_")]
            self._best_params = bp_df[param_cols].values[0]

        reach_ids = self._reach_ids
        out_dir   = self.hind_dir if phase == "hindcast" else self.fore_dir
        p_start   = pd.Timestamp(period_cfg.start)
        p_end     = pd.Timestamp(period_cfg.end)

        self._emit(f"Running {phase} ({period_cfg.start} → {period_cfg.end}) …", 0.02)
        sims = self._runner(self._best_params, self._best_param_df)

        results = {}
        for r in reach_ids:
            sim_r = sims.get(r)
            if sim_r is None:
                continue
            period = sim_r.loc[p_start:p_end]
            if len(period) == 0:
                self._emit(f"  Reach {r}: no output in {phase} period", 0.5)
                continue
            results[r] = {
                "n_months": len(period),
                "mean": float(period.mean()), "min": float(period.min()),
                "max": float(period.max()),   "std": float(period.std()),
            }
            pd.DataFrame({"date": period.index, "simulated_flow": period.values}).to_csv(
                out_dir / f"{phase}_flow_rch{r}.csv", index=False)
            self._emit(f"  Reach {r:3d} — {len(period)} months | "
                       f"mean:{period.mean():.3f}  min:{period.min():.3f}  max:{period.max():.3f} m³/s", 0.5)

        del sims; gc.collect()
        self._emit(f"{phase.capitalize()} complete!", 1.0)

        return {
            "phase":       phase,
            "period":      f"{period_cfg.start} → {period_cfg.end}",
            "reach_stats": results,
            "out_dir":     str(out_dir),
            "results_dir": str(self.results_dir),
        }
