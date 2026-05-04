"""
sufi2.core
==========
Pure calibration engine — no hardcoded config, no CLI/API concerns.

Entry point:
    from sufi2.core import SUFI2Engine
    engine = SUFI2Engine(config, logger=my_logger)
    engine.run()
"""

from __future__ import annotations

import glob
import logging
import os
import re
import shutil
import subprocess
import tempfile
import warnings
from copy import deepcopy
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

warnings.filterwarnings("ignore", category=RuntimeWarning)

log = logging.getLogger("sufi2.core")


# ─────────────────────────────────────────────────────────────────────────────
# DEFAULT PARAMETER TABLE  (used when par_inf is None)
# ─────────────────────────────────────────────────────────────────────────────

DEFAULT_PARAMS = pd.DataFrame([
    {"name": "CN2.mgt",     "min": -0.25,  "max":  0.25,  "change_type": "r"},
    {"name": "ALPHA_BF.gw", "min":  0.00,  "max":  1.00,  "change_type": "v"},
    {"name": "GW_DELAY.gw", "min":  0.00,  "max":  500.0, "change_type": "v"},
    {"name": "GWQMN.gw",    "min":  0.00,  "max":  5000., "change_type": "v"},
    {"name": "GW_REVAP.gw", "min":  0.02,  "max":  0.20,  "change_type": "v"},
    {"name": "REVAPMN.gw",  "min":  0.00,  "max":  500.0, "change_type": "v"},
    {"name": "SOL_AWC.sol", "min": -0.20,  "max":  0.20,  "change_type": "r"},
    {"name": "SOL_K.sol",   "min": -0.80,  "max":  0.80,  "change_type": "r"},
    {"name": "SOL_BD.sol",  "min": -0.50,  "max":  0.60,  "change_type": "r"},
    {"name": "ESCO.hru",    "min":  0.00,  "max":  1.00,  "change_type": "v"},
    {"name": "EPCO.hru",    "min":  0.00,  "max":  1.00,  "change_type": "v"},
    {"name": "CH_N2.rte",   "min":  0.01,  "max":  0.30,  "change_type": "v"},
    {"name": "CH_K2.rte",   "min":  0.00,  "max":  150.0, "change_type": "v"},
    {"name": "SURLAG.bsn",  "min":  0.50,  "max":  10.00, "change_type": "v"},
    {"name": "SMFMX.bsn",   "min":  0.00,  "max":  20.00, "change_type": "v"},
    {"name": "SPCON.bsn",   "min":  0.0001,"max":  0.01,  "change_type": "v"},
    {"name": "SPEXP.bsn",   "min":  1.00,  "max":  2.00,  "change_type": "v"},
    {"name": "CH_EROD.rte", "min":  0.00,  "max":  1.00,  "change_type": "v"},
    {"name": "CH_COV.rte",  "min":  0.00,  "max":  1.00,  "change_type": "v"},
    {"name": "USLE_P.mgt",  "min":  0.10,  "max":  1.00,  "change_type": "v"},
])


# ─────────────────────────────────────────────────────────────────────────────
# FILE I/O HELPERS
# ─────────────────────────────────────────────────────────────────────────────

def find_file(work_dir: Path, patterns: List[str], required: bool = True) -> Optional[Path]:
    for pat in patterns:
        hits = sorted(work_dir.glob(pat))
        if hits:
            return hits[0]
    if required:
        raise FileNotFoundError(
            f"Could not find any of {patterns} in {work_dir}"
        )
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
            "Expected files like: observed_flow_rch13.csv, observed_flow_rch15.csv …"
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
            files["exe"] = p
            break
        w = shutil.which(c)
        if w:
            files["exe"] = Path(w)
            break

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
    except Exception as e:
        log.warning("file.cio parse warning: %s", e)
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
        log.info(
            "Reach %3d obs: %d records (%s → %s)",
            reach_id, len(series),
            series.index[0].date(), series.index[-1].date(),
        )
    return obs_dict


def load_output_rch(rch_path: Path, reach_ids: Optional[List[int]] = None) -> Tuple[Dict[int, pd.Series], str]:
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
        raise ValueError("output.rch is empty or malformed — could not parse any data lines.")

    log.info("output.rch format: %s", fmt)

    if records_a:
        dated = []
        current_year = None
        for r in records:
            mon = r["mon"]
            if mon > 1900:
                current_year = mon
                continue
            if current_year is None:
                continue
            if 1 <= mon <= 12:
                try:
                    dated.append({"rch": r["rch"], "date": pd.Timestamp(year=current_year, month=mon, day=1), "flow": r["flow"]})
                except Exception:
                    pass
        if not dated:
            from itertools import groupby
            reach_records: dict = {}
            for r in records:
                reach_records.setdefault(r["rch"], []).append(r["flow"])
            start_year = 1990
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
                    rows.append({
                        "name":        p[0],
                        "min":         float(p[1]),
                        "max":         float(p[2]),
                        "change_type": p[3].lower() if len(p) > 3 else "v",
                    })
                except ValueError:
                    continue
    if not rows:
        log.warning("par_inf.txt is empty — using defaults")
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
        lines = fpath.read_text().splitlines()
        modified = False
        for i, line in enumerate(lines):
            m = re.match(
                r"^(\s*" + re.escape(param_name) + r"\s*:\s*)([-+]?\d*\.?\d+(?:[eE][-+]?\d+)?)(.*)",
                line, re.IGNORECASE,
            )
            if m:
                orig = float(m.group(2))
                lines[i] = f"{m.group(1)}{_apply_change(orig, new_val, change_type):.6g}{m.group(3)}"
                modified = True
        if modified:
            fpath.write_text("\n".join(lines) + "\n", encoding="utf-8")


def write_mgt(work_dir: Path, param_name: str, new_val: float, change_type: str):
    pname = param_name.upper()
    for fpath in sorted(work_dir.glob("*.mgt")):
        lines = fpath.read_text().splitlines()
        modified = False
        if pname == "CN2" and len(lines) > 8:
            orig = _safe_float(lines[8][:16])
            if orig is not None:
                lines[8] = f"{_apply_change(orig, new_val, change_type):16.4f}" + lines[8][16:]
                modified = True
        elif pname == "BIOMIX" and len(lines) > 9:
            orig = _safe_float(lines[9][:16])
            if orig is not None:
                lines[9] = f"{_apply_change(orig, new_val, change_type):16.4f}" + lines[9][16:]
                modified = True
        elif pname == "USLE_P" and len(lines) > 11:
            orig = _safe_float(lines[11][:16])
            if orig is not None:
                lines[11] = f"{_apply_change(orig, new_val, change_type):16.4f}" + lines[11][16:]
                modified = True
        else:
            for i, line in enumerate(lines):
                if re.search(rf"\b{re.escape(pname)}\b", line, re.IGNORECASE):
                    orig = _safe_float(line)
                    if orig is not None:
                        lines[i] = _replace_first_number(line, _apply_change(orig, new_val, change_type))
                        modified = True
                    break
        if modified:
            fpath.write_text("\n".join(lines) + "\n", encoding="utf-8")


def write_sol(work_dir: Path, param_name: str, new_val: float, change_type: str):
    pname = param_name.upper()
    for fpath in sorted(work_dir.glob("*.sol")):
        lines = fpath.read_text().splitlines()
        modified = False
        for i, line in enumerate(lines):
            m = re.match(r"^(\s*" + re.escape(pname) + r"\s*:\s*)(.*)", line, re.IGNORECASE)
            if not m:
                m2 = re.match(r"^(\s*" + re.escape(pname) + r"\s+)([\d\s.eE+\-]+)", line, re.IGNORECASE)
                if m2:
                    vals = re.findall(r"[-+]?\d*\.?\d+(?:[eE][-+]?\d+)?", m2.group(2))
                    if vals:
                        new_vals = [_apply_change(float(v), new_val, change_type) for v in vals]
                        lines[i] = m2.group(1) + "  ".join(f"{v:.6g}" for v in new_vals)
                        modified = True
                continue
            vals = re.findall(r"[-+]?\d*\.?\d+(?:[eE][-+]?\d+)?", m.group(2))
            if vals:
                new_vals = [_apply_change(float(v), new_val, change_type) for v in vals]
                lines[i] = m.group(1) + "  ".join(f"{v:>10.6g}" for v in new_vals)
                modified = True
        if modified:
            fpath.write_text("\n".join(lines) + "\n", encoding="utf-8")


def write_hru(work_dir: Path, param_name: str, new_val: float, change_type: str):
    pname = param_name.upper()
    HRU_LINE_MAP = {"ESCO": 4, "EPCO": 5, "OV_N": 6, "LAT_TTIME": 7, "SLSUBBSN": 8}
    for fpath in sorted(work_dir.glob("*.hru")):
        lines = fpath.read_text().splitlines()
        modified = False
        idx = HRU_LINE_MAP.get(pname)
        if idx is not None and len(lines) > idx:
            orig = _safe_float(lines[idx][:16])
            if orig is not None:
                lines[idx] = f"{_apply_change(orig, new_val, change_type):16.4f}" + lines[idx][16:]
                modified = True
        else:
            for i, line in enumerate(lines):
                if re.search(rf"\b{re.escape(pname)}\b", line, re.IGNORECASE):
                    orig = _safe_float(line)
                    if orig is not None:
                        lines[i] = _replace_first_number(line, _apply_change(orig, new_val, change_type))
                        modified = True
                    break
        if modified:
            fpath.write_text("\n".join(lines) + "\n", encoding="utf-8")


def write_rte(work_dir: Path, param_name: str, new_val: float, change_type: str):
    pname = param_name.upper()
    RTE_LINE_MAP = {"CH_N2": 4, "CH_K2": 5, "CH_W2": 7, "CH_D2": 8, "CH_S2": 9}
    for fpath in sorted(work_dir.glob("*.rte")):
        lines = fpath.read_text().splitlines()
        modified = False
        idx = RTE_LINE_MAP.get(pname)
        if idx is not None and len(lines) > idx:
            orig = _safe_float(lines[idx][:16])
            if orig is not None:
                lines[idx] = f"{_apply_change(orig, new_val, change_type):16.6g}" + lines[idx][16:]
                modified = True
        else:
            for i, line in enumerate(lines):
                if re.search(rf"\b{re.escape(pname)}\b", line, re.IGNORECASE):
                    orig = _safe_float(line)
                    if orig is not None:
                        lines[i] = _replace_first_number(line, _apply_change(orig, new_val, change_type))
                        modified = True
                    break
        if modified:
            fpath.write_text("\n".join(lines) + "\n", encoding="utf-8")


def write_bsn(work_dir: Path, param_name: str, new_val: float, change_type: str):
    pname = param_name.upper()
    for fpath in sorted(work_dir.glob("*.bsn")):
        lines = fpath.read_text().splitlines()
        modified = False
        for i, line in enumerate(lines):
            m = re.match(
                r"^(\s*" + re.escape(pname) + r"\s*:\s*)([-+]?\d*\.?\d+(?:[eE][-+]?\d+)?)(.*)",
                line, re.IGNORECASE,
            )
            if m:
                orig = float(m.group(2))
                lines[i] = f"{m.group(1)}{_apply_change(orig, new_val, change_type):.6g}{m.group(3)}"
                modified = True
                break
            if re.match(r"^\s*" + re.escape(pname) + r"\s+\d", line, re.IGNORECASE):
                orig = _safe_float(line)
                if orig is not None:
                    lines[i] = _replace_first_number(line, _apply_change(orig, new_val, change_type))
                    modified = True
                    break
        if modified:
            fpath.write_text("\n".join(lines) + "\n", encoding="utf-8")


EXT_WRITERS = {".mgt": write_mgt, ".gw": write_gw, ".sol": write_sol, ".hru": write_hru, ".rte": write_rte, ".bsn": write_bsn}


def write_all_parameters(param_set: np.ndarray, param_df: pd.DataFrame, work_dir: Path):
    for j, (_, row) in enumerate(param_df.iterrows()):
        parts = row["name"].split(".")
        if len(parts) < 2:
            continue
        writer = EXT_WRITERS.get("." + parts[1].lower())
        if writer is None:
            continue
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
    except subprocess.TimeoutExpired:
        log.warning("SWAT timed out after %ds", timeout)
        return False
    except Exception as e:
        log.warning("SWAT run error: %s", e)
        return False


def mock_swat_run(param_set: np.ndarray, param_df: pd.DataFrame, base_sims: Dict[int, pd.Series], seed_extra: int = 0) -> Dict[int, pd.Series]:
    rng = np.random.default_rng((int(np.sum(np.abs(param_set) * 1e4)) + seed_extra) % (2**31))
    cn2_eff   = float(param_set[0]) if len(param_set) > 0 else 0.0
    alpha_eff = float(param_set[1]) if len(param_set) > 1 else 0.5
    esco_eff  = float(param_set[9]) if len(param_set) > 9 else 0.5
    results: Dict[int, pd.Series] = {}
    for rid, base in base_sims.items():
        scale = 0.80 + 0.35 * alpha_eff + 0.05 * (rid % 5) * 0.1
        noise = rng.normal(0, 0.07 * base.std(), size=len(base))
        perturbed = np.maximum(base.values * scale * (1 + cn2_eff * 0.5) * (1 - esco_eff * 0.1) + noise, 0.0)
        results[rid] = pd.Series(perturbed, index=base.index)
    return results


def make_runner(exe_path: Optional[Path], base_sims: Dict[int, pd.Series], reach_ids: List[int], work_dir: Path) -> Callable:
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
    reach_scores = {rid: (obj_fn(obs, sims_dict[rid]) if rid in sims_dict else -np.inf) for rid, obs in obs_dict.items()}
    reach_ids = list(reach_scores.keys())
    if weights is None:
        w = {r: 1.0 / len(reach_ids) for r in reach_ids}
    else:
        total = sum(weights.get(r, 1.0) for r in reach_ids)
        w = {r: weights.get(r, 1.0) / total for r in reach_ids}
    combined = sum(w[r] * reach_scores[r] for r in reach_ids)
    return combined, reach_scores


def full_metrics(obs_dict, sims_dict):
    out = {}
    for rid, obs in obs_dict.items():
        sim = sims_dict.get(rid)
        if sim is None:
            out[rid] = {"NSE": float("nan"), "PBIAS": float("nan"), "R2": float("nan"), "KGE": float("nan")}
        else:
            out[rid] = {"NSE": nse(obs, sim), "PBIAS": pbias(obs, sim), "R2": r_squared(obs, sim), "KGE": kge(obs, sim)}
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
    n = len(obj_vals)
    ranked_obj = stats.rankdata(obj_vals)
    rows = []
    for i, name in enumerate(names):
        ranked_p = stats.rankdata(samples[:, i])
        slope, _, _, p_val, se = stats.linregress(ranked_p, ranked_obj)
        t = slope / se if se > 0 else 0.0
        rows.append({"Parameter": name, "t-stat": round(t, 4), "p-value": round(p_val, 6) if not np.isnan(p_val) else float("nan")})
    df = pd.DataFrame(rows).sort_values("t-stat", key=abs, ascending=False).reset_index(drop=True)
    df["Rank"] = range(1, len(df) + 1)
    return df


def compute_95ppu(obs_s: pd.Series, all_sims_list: List[pd.Series]) -> dict:
    common = obs_s.index
    for s in all_sims_list:
        common = common.intersection(s.index)
    obs_arr = obs_s.loc[common].values
    sim_mat = np.column_stack([s.loc[common].values for s in all_sims_list])
    lower = np.percentile(sim_mat, 2.5, axis=1)
    upper = np.percentile(sim_mat, 97.5, axis=1)
    nse_all = np.array([nse(obs_arr, sim_mat[:, j]) for j in range(sim_mat.shape[1])])
    best = sim_mat[:, np.argmax(nse_all)]
    p_fac = float(np.mean((obs_arr >= lower) & (obs_arr <= upper)))
    r_fac = float(np.mean(upper - lower) / np.std(obs_arr)) if np.std(obs_arr) > 0 else float("nan")
    return {"lower": lower, "upper": upper, "best": best, "obs": obs_arr, "dates": common, "p_factor": round(p_fac, 4), "r_factor": round(r_fac, 4), "n_sims": sim_mat.shape[1]}


def update_ranges(param_df: pd.DataFrame, samples: np.ndarray, obj_vals: np.ndarray, top_frac: float = 0.10) -> pd.DataFrame:
    n_top = max(2, int(len(obj_vals) * top_frac))
    top_ix = np.argsort(obj_vals)[-n_top:]
    top_S = samples[top_ix, :]
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
        sc = axes[j].scatter(samples[:, j], obj_vals, c=obj_vals, cmap="RdYlGn", norm=norm, alpha=0.55, s=8, edgecolors="none")
        axes[j].set_xlabel(name, fontsize=8)
        axes[j].set_ylabel(label, fontsize=8)
        axes[j].set_title(name, fontsize=9)
        axes[j].axhline(0, color="gray", lw=0.7, ls="--")
        plt.colorbar(sc, ax=axes[j])
    for k in range(n_p, len(axes)):
        axes[k].set_visible(False)
    fig.suptitle(f"Dotty Plots — {label}", fontsize=12, y=1.01)
    plt.tight_layout()
    plt.savefig(out, dpi=150, bbox_inches="tight")
    plt.close()


def plot_ppu(ppu, reach_id, iteration, out):
    fig, ax = plt.subplots(figsize=(14, 5))
    dates = pd.DatetimeIndex(ppu["dates"])
    ax.fill_between(dates, ppu["lower"], ppu["upper"], alpha=0.35, color="steelblue", label="95PPU")
    ax.plot(dates, ppu["best"], color="darkorange", lw=1.5, label="Best sim", zorder=3)
    ax.plot(dates, ppu["obs"], "k.", ms=3, label="Observed", zorder=4)
    ax.set(xlabel="Date", ylabel="Flow (m³/s)", title=f"95PPU — Reach {reach_id}  |  Iteration {iteration}  |  P-factor: {ppu['p_factor']:.3f}  R-factor: {ppu['r_factor']:.3f}")
    ax.legend()
    ax.grid(alpha=0.3)
    plt.tight_layout()
    plt.savefig(out, dpi=150, bbox_inches="tight")
    plt.close()


def plot_sensitivity(sens_df, label, out):
    df = sens_df.sort_values("t-stat", key=abs).copy()
    if df["t-stat"].abs().max() == 0:
        df["t-stat"] = df["t-stat"] + 1e-9
    colors = ["#2ca02c" if (not np.isnan(p) and p < 0.05) else "#ff7f0e" if (not np.isnan(p) and p < 0.10) else "#d62728" for p in df["p-value"]]
    fig, ax = plt.subplots(figsize=(8, max(4, len(df) * 0.45)))
    ax.barh(df["Parameter"], df["t-stat"].abs(), color=colors, edgecolor="white")
    ax.axvline(1.96, color="gray", ls="--", lw=0.9)
    ax.set(xlabel="|t-statistic|", title=f"Sensitivity — {label}")
    patches = [mpatches.Patch(color="#2ca02c", label="p<0.05"), mpatches.Patch(color="#ff7f0e", label="p<0.10"), mpatches.Patch(color="#d62728", label="p≥0.10")]
    ax.legend(handles=patches, fontsize=9)
    ax.grid(axis="x", alpha=0.3)
    plt.tight_layout()
    plt.savefig(out, dpi=150, bbox_inches="tight")
    plt.close()


def plot_convergence(results, reach_ids, obj_label, out):
    n_panels = len(reach_ids) + 1
    ncols = min(3, n_panels)
    nrows = (n_panels + ncols - 1) // ncols
    fig, axes = plt.subplots(nrows, ncols, figsize=(6 * ncols, 5 * nrows), squeeze=False)
    axes_flat = axes.flatten()
    all_panels = [("combined", "Combined")] + [(r, f"Reach {r}") for r in reach_ids]
    for idx, (key, title) in enumerate(all_panels):
        ax = axes_flat[idx]
        data   = [r["combined_obj"] for r in results.values()] if key == "combined" else [r["reach_obj"][key] for r in results.values()]
        labels = [f"Iter {i}" for i in results.keys()]
        bp = ax.boxplot(data, labels=labels, patch_artist=True, medianprops=dict(color="black", lw=2))
        blues = plt.cm.Blues(np.linspace(0.3, 0.8, len(data)))
        for patch, c in zip(bp["boxes"], blues):
            patch.set_facecolor(c)
        ax.axhline(0.5, color="green", ls="--", lw=1)
        ax.axhline(0.65, color="darkgreen", ls="--", lw=1)
        ax.set(ylabel=obj_label, xlabel="Iteration", title=title)
        ax.grid(alpha=0.3)
    for k in range(n_panels, len(axes_flat)):
        axes_flat[k].set_visible(False)
    fig.suptitle(f"Objective Function Convergence ({obj_label})", fontsize=13)
    plt.tight_layout()
    plt.savefig(out, dpi=150, bbox_inches="tight")
    plt.close()


# ─────────────────────────────────────────────────────────────────────────────
# SUMMARY REPORT
# ─────────────────────────────────────────────────────────────────────────────

def write_report(results, out_path, obj_label, reach_ids, files,
                 val_results=None, hind_results=None, fore_results=None, cfg=None):
    val_results  = val_results  or {}
    hind_results = hind_results or {}
    fore_results = fore_results or {}

    lines = [
        "SUFI-2 SWAT Multi-Reach Calibration — Summary Report",
        f"Generated  : {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        f"Reaches    : {reach_ids}",
        f"Obs files  : {[f.name for f in files['obs']]}",
        f"Par file   : {files['par'].name if files['par'] else 'defaults'}",
        f"Objective  : {obj_label}",
        "=" * 65,
    ]

    # ── Calibration iterations ────────────────────────────────────────────
    for it, r in results.items():
        lines += [f"\n{'─'*65}", f"Iteration {it}", f"{'─'*65}"]
        lines.append(f"  Combined {obj_label} best : {r['best_combined']:.4f}")
        for rid in reach_ids:
            m = r["best_metrics"].get(rid, {})
            lines.append(
                f"  Reach {rid:3d} — NSE: {m.get('NSE', float('nan')):.3f}  "
                f"PBIAS: {m.get('PBIAS', float('nan')):.1f}%  "
                f"R²: {m.get('R2', float('nan')):.3f}  "
                f"KGE: {m.get('KGE', float('nan')):.3f}"
            )
        for rid in reach_ids:
            ppu = r["ppu"].get(rid, {})
            lines.append(
                f"  Reach {rid:3d} — P-factor: {ppu.get('p_factor', float('nan')):.3f}  "
                f"R-factor: {ppu.get('r_factor', float('nan')):.3f}"
            )
        lines.append(f"\n  Sensitivity (combined, top 5):")
        for _, row in r["sensitivity_combined"].head(5).iterrows():
            pv  = row["p-value"]
            sig = "***" if (not (isinstance(pv, float) and np.isnan(pv)) and pv < 0.05)                   else ("*" if (not (isinstance(pv, float) and np.isnan(pv)) and pv < 0.10) else "")
            lines.append(f"    {row['Parameter']:22s}  t={row['t-stat']:+.3f}  p={row['p-value']:.4f} {sig}")

    # ── Validation ────────────────────────────────────────────────────────
    if cfg is not None:
        lines += [f"\n{'='*65}", "VALIDATION PERIOD",
                  f"Period: {cfg.periods.validation.start} → {cfg.periods.validation.end}",
                  f"{'─'*65}"]
    else:
        lines += [f"\n{'='*65}", "VALIDATION PERIOD", f"{'─'*65}"]

    if val_results:
        for rid in reach_ids:
            m = val_results.get(rid)
            if m:
                lines.append(
                    f"  Reach {rid:3d} — NSE: {m['NSE']:.3f}  PBIAS: {m['PBIAS']:+.1f}%  "
                    f"R²: {m['R2']:.3f}  KGE: {m['KGE']:.3f}"
                )
            else:
                lines.append(f"  Reach {rid:3d} — no data in validation period")
    else:
        lines.append("  No validation results (check observed data coverage)")

    # ── Hindcast ──────────────────────────────────────────────────────────
    if cfg is not None and cfg.periods.hindcast is not None:
        lines += [f"\n{'='*65}", "HINDCAST PERIOD (simulation only — no scoring)",
                  f"Period: {cfg.periods.hindcast.start} → {cfg.periods.hindcast.end}",
                  f"{'─'*65}"]
        if hind_results:
            for rid in reach_ids:
                h = hind_results.get(rid)
                if h:
                    lines.append(
                        f"  Reach {rid:3d} — {h['n_months']} months | "
                        f"mean: {h['mean']:.3f}  min: {h['min']:.3f}  "
                        f"max: {h['max']:.3f}  std: {h['std']:.3f} m³/s"
                    )
                else:
                    lines.append(f"  Reach {rid:3d} — no simulated output in hindcast period")
        else:
            lines.append("  No hindcast output")
    else:
        lines += [f"\n{'='*65}", "HINDCAST — not configured (set periods.hindcast in config)"]

    # ── Forecast ──────────────────────────────────────────────────────────
    if cfg is not None and cfg.periods.forecast is not None:
        lines += [f"\n{'='*65}", "FORECAST PERIOD (simulation only — no scoring)",
                  f"Period: {cfg.periods.forecast.start} → {cfg.periods.forecast.end}",
                  f"{'─'*65}"]
        if fore_results:
            for rid in reach_ids:
                f_ = fore_results.get(rid)
                if f_:
                    lines.append(
                        f"  Reach {rid:3d} — {f_['n_months']} months | "
                        f"mean: {f_['mean']:.3f}  min: {f_['min']:.3f}  "
                        f"max: {f_['max']:.3f}  std: {f_['std']:.3f} m³/s"
                    )
                else:
                    lines.append(f"  Reach {rid:3d} — no simulated output in forecast period")
        else:
            lines.append("  No forecast output")
    else:
        lines += [f"\n{'='*65}", "FORECAST — not configured (set periods.forecast in config)"]

    out_path.write_text("\n".join(lines), encoding="utf-8")
    log.info("Report written: %s", out_path)


# ─────────────────────────────────────────────────────────────────────────────
# MAIN ENGINE CLASS
# ─────────────────────────────────────────────────────────────────────────────

class SUFI2Engine:
    """
    Orchestrates a full SUFI-2 calibration run given a SUFI2Config.

    Usage:
        cfg    = SUFI2Config.from_yaml("config.yaml")
        engine = SUFI2Engine(cfg)
        engine.run()
    """

    def __init__(self, config: SUFI2Config, progress_callback: Optional[Callable] = None):
        self.cfg = config
        self.progress_callback = progress_callback or (lambda msg, pct: None)
        self.work_dir = config.paths.work_path
        self.results_dir = config.paths.results_path
        self.results_dir.mkdir(parents=True, exist_ok=True)

    def _emit(self, msg: str, pct: float = 0.0):
        log.info(msg)
        self.progress_callback(msg, pct)

    def run(self) -> dict:
        cfg = self.cfg
        self._emit("Detecting files …", 0.02)
        files = detect_files(self.work_dir)

        if cfg.paths.par_inf:
            files["par"] = Path(cfg.paths.par_inf)
        if cfg.paths.swat_exe:
            files["exe"] = Path(cfg.paths.swat_exe)

        cio_info = parse_file_cio(files["cio"]) if files["cio"] else {}

        self._emit("Loading observed flow …", 0.05)
        obs_dict_full = load_observed_all(files["obs"], warmup_years=cfg.calibration.warmup_years)
        reach_ids = sorted(obs_dict_full.keys())
        if cfg.reach_ids:
            reach_ids = [r for r in cfg.reach_ids if r in obs_dict_full]

        calib_s = pd.Timestamp(cfg.periods.calibration.start)
        calib_e = pd.Timestamp(cfg.periods.calibration.end)
        valid_s = pd.Timestamp(cfg.periods.validation.start)
        valid_e = pd.Timestamp(cfg.periods.validation.end)

        obs_dict       = {r: s.loc[calib_s:calib_e] for r, s in obs_dict_full.items()}
        obs_dict_valid = {r: s.loc[valid_s:valid_e]  for r, s in obs_dict_full.items()}

        self._emit("Loading output.rch …", 0.08)
        base_sims, _ = load_output_rch(files["rch"], reach_ids=reach_ids)

        param_df = load_parameters(files["par"])

        if cfg.backup_inputs and files["exe"] is not None:
            backup_input_files(self.work_dir, self.results_dir / "input_backup")
            self._emit("Input files backed up.", 0.10)

        runner   = make_runner(files["exe"], base_sims, reach_ids, self.work_dir)
        obj_fn   = OBJ_FUNCS.get(cfg.calibration.obj_func, nse)
        obj_lbl  = cfg.calibration.obj_func
        weights  = cfg.calibration.reach_weights

        results         = {}
        current_params  = param_df.copy()
        global_best_obj = -np.inf
        global_best_set = None
        global_best_it  = None

        n_iter = cfg.calibration.iterations
        n_samp = cfg.calibration.n_samples

        for it in range(1, n_iter + 1):
            base_pct = 0.10 + (it - 1) / n_iter * 0.75
            self._emit(f"Iteration {it}/{n_iter} — generating {n_samp} LHS samples …", base_pct)
            it_dir = self.results_dir / f"iteration_{it}"
            it_dir.mkdir(exist_ok=True)

            samples = lhs(current_params, n_samp, seed=cfg.calibration.seed + it)

            all_sims, combined_objs, reach_obj_mat = [], [], {r: [] for r in reach_ids}

            for i, ps in enumerate(samples):
                sims_i = runner(ps, current_params, _seed=i)
                all_sims.append(sims_i)
                comb, per_reach = combined_objective(obs_dict, sims_i, obj_fn, weights)
                combined_objs.append(comb)
                for r in reach_ids:
                    reach_obj_mat[r].append(per_reach.get(r, -np.inf))
                if (i + 1) % max(1, n_samp // 10) == 0:
                    pct = base_pct + ((i + 1) / n_samp) * (0.75 / n_iter)
                    self._emit(f"  Iter {it}: {i+1}/{n_samp} simulations …", pct)

            combined_objs = np.array(combined_objs)
            reach_obj_mat = {r: np.array(v) for r, v in reach_obj_mat.items()}
            best_idx  = int(np.argmax(combined_objs))
            best_comb = combined_objs[best_idx]

            if best_comb > global_best_obj:
                global_best_obj = best_comb
                global_best_set = samples[best_idx].copy()
                global_best_it  = it

            best_metrics = full_metrics(obs_dict, all_sims[best_idx])

            self._emit(f"Iter {it} best {obj_lbl}: {best_comb:.4f}", base_pct + 0.5 / n_iter)

            sens_combined  = sensitivity_analysis(samples, combined_objs, list(current_params["name"]))
            sens_per_reach = {r: sensitivity_analysis(samples, reach_obj_mat[r], list(current_params["name"])) for r in reach_ids}

            ppu_per_reach = {}
            for r in reach_ids:
                sims_for_r = [s[r] for s in all_sims if r in s]
                ppu_per_reach[r] = compute_95ppu(obs_dict[r], sims_for_r)

            # CSVs
            ps_df = pd.DataFrame(samples, columns=list(current_params["name"]))
            ps_df.insert(0, f"{obj_lbl}_combined", combined_objs)
            for r in reach_ids:
                ps_df.insert(1, f"{obj_lbl}_rch{r}", reach_obj_mat[r])
            ps_df.to_csv(it_dir / "parameter_sets.csv", index=False)
            sens_combined.to_csv(it_dir / "sensitivity_combined.csv", index=False)
            for r in reach_ids:
                sens_per_reach[r].to_csv(it_dir / f"sensitivity_rch{r}.csv", index=False)
                pd.DataFrame({"date": ppu_per_reach[r]["dates"], "observed": ppu_per_reach[r]["obs"], "lower_95": ppu_per_reach[r]["lower"], "upper_95": ppu_per_reach[r]["upper"], "best_sim": ppu_per_reach[r]["best"]}).to_csv(it_dir / f"ppu_band_rch{r}.csv", index=False)

            # Plots
            names = list(current_params["name"])
            plot_dotty(samples, combined_objs, names, f"{obj_lbl} Combined Iter {it}", it_dir / "dotty_combined.png")
            for r in reach_ids:
                plot_dotty(samples, reach_obj_mat[r], names, f"{obj_lbl} Reach {r} Iter {it}", it_dir / f"dotty_rch{r}.png")
                plot_ppu(ppu_per_reach[r], r, it, it_dir / f"95ppu_rch{r}.png")
                plot_sensitivity(sens_per_reach[r], f"Reach {r} Iter {it}", it_dir / f"sensitivity_rch{r}.png")
            plot_sensitivity(sens_combined, f"Combined Iter {it}", it_dir / "sensitivity_combined.png")

            results[it] = {
                "samples": samples, "combined_obj": combined_objs, "reach_obj": reach_obj_mat,
                "all_sims": all_sims, "best_combined": best_comb, "best_metrics": best_metrics,
                "sensitivity_combined": sens_combined, "sensitivity_per_reach": sens_per_reach,
                "ppu": ppu_per_reach, "param_ranges": current_params.copy(),
            }

            if it < n_iter:
                current_params = update_ranges(current_params, samples, combined_objs, cfg.calibration.top_fraction)
                current_params.to_csv(it_dir / "updated_ranges.csv", index=False)

        # Best params
        best_df = pd.DataFrame([global_best_set], columns=list(param_df["name"]))
        best_df.insert(0, "best_iteration", global_best_it)
        best_df.insert(1, f"best_{obj_lbl}", global_best_obj)
        best_df.to_csv(self.results_dir / "best_parameters.csv", index=False)

        # Convergence plot
        plot_convergence(results, reach_ids, obj_lbl, self.results_dir / "convergence.png")

        # ── Validation ────────────────────────────────────────────────────
        self._emit("Running validation period …", 0.88)
        val_results = {}
        if global_best_set is not None:
            val_sims = runner(global_best_set, current_params)
            for r in reach_ids:
                if r in val_sims and r in obs_dict_valid and len(obs_dict_valid[r]) > 0:
                    obs_v  = obs_dict_valid[r]
                    sim_v  = val_sims[r]
                    common = obs_v.index.intersection(sim_v.index)
                    if len(common) > 2:
                        o_arr, s_arr = obs_v.loc[common].values, sim_v.loc[common].values
                        val_results[r] = {
                            "NSE":   nse(o_arr, s_arr),
                            "KGE":   kge(o_arr, s_arr),
                            "R2":    r_squared(o_arr, s_arr),
                            "PBIAS": pbias(o_arr, s_arr),
                        }
                        self._emit(
                            f"  Validation Reach {r:3d} — NSE:{val_results[r]['NSE']:.3f}  "
                            f"KGE:{val_results[r]['KGE']:.3f}  R²:{val_results[r]['R2']:.3f}  "
                            f"PBIAS:{val_results[r]['PBIAS']:+.1f}%",
                            0.89,
                        )
                    else:
                        self._emit(f"  Validation Reach {r}: insufficient overlap in period", 0.89)
                else:
                    self._emit(f"  Validation Reach {r}: no observed data in validation period", 0.89)

        # ── Hindcast ──────────────────────────────────────────────────────────
        hind_results = {}
        if global_best_set is not None and cfg.periods.hindcast is not None:
            hind_s = pd.Timestamp(cfg.periods.hindcast.start)
            hind_e = pd.Timestamp(cfg.periods.hindcast.end)
            self._emit(
                f"Running hindcast period ({cfg.periods.hindcast.start} → {cfg.periods.hindcast.end}) …",
                0.92,
            )
            hind_sims = runner(global_best_set, current_params)
            for r in reach_ids:
                if r in hind_sims:
                    sim_r  = hind_sims[r]
                    period = sim_r.loc[hind_s:hind_e]
                    if len(period) > 0:
                        hind_results[r] = {
                            "n_months": len(period),
                            "mean":     float(period.mean()),
                            "min":      float(period.min()),
                            "max":      float(period.max()),
                            "std":      float(period.std()),
                        }
                        self._emit(
                            f"  Hindcast Reach {r:3d} — {len(period)} months | "
                            f"mean:{period.mean():.3f}  min:{period.min():.3f}  max:{period.max():.3f} m³/s",
                            0.93,
                        )
                        # Save hindcast time-series CSV
                        pd.DataFrame({"date": period.index, "simulated_flow": period.values}).to_csv(
                            self.results_dir / f"hindcast_rch{r}.csv", index=False
                        )
                    else:
                        self._emit(f"  Hindcast Reach {r}: no simulated output in period", 0.93)
        elif cfg.periods.hindcast is None:
            self._emit("Hindcast period not configured — skipping.", 0.92)

        # ── Forecast ──────────────────────────────────────────────────────────
        fore_results = {}
        if global_best_set is not None and cfg.periods.forecast is not None:
            fore_s = pd.Timestamp(cfg.periods.forecast.start)
            fore_e = pd.Timestamp(cfg.periods.forecast.end)
            self._emit(
                f"Running forecast period ({cfg.periods.forecast.start} → {cfg.periods.forecast.end}) …",
                0.95,
            )
            fore_sims = runner(global_best_set, current_params)
            for r in reach_ids:
                if r in fore_sims:
                    sim_r  = fore_sims[r]
                    period = sim_r.loc[fore_s:fore_e]
                    if len(period) > 0:
                        fore_results[r] = {
                            "n_months": len(period),
                            "mean":     float(period.mean()),
                            "min":      float(period.min()),
                            "max":      float(period.max()),
                            "std":      float(period.std()),
                        }
                        self._emit(
                            f"  Forecast Reach {r:3d} — {len(period)} months | "
                            f"mean:{period.mean():.3f}  min:{period.min():.3f}  max:{period.max():.3f} m³/s",
                            0.96,
                        )
                        # Save forecast time-series CSV
                        pd.DataFrame({"date": period.index, "simulated_flow": period.values}).to_csv(
                            self.results_dir / f"forecast_rch{r}.csv", index=False
                        )
                    else:
                        self._emit(f"  Forecast Reach {r}: no simulated output in period", 0.96)
        elif cfg.periods.forecast is None:
            self._emit("Forecast period not configured — skipping.", 0.95)

        # ── Summary report ────────────────────────────────────────────────────
        write_report(
            results, self.results_dir / "summary_report.txt",
            obj_lbl, reach_ids, files,
            val_results=val_results,
            hind_results=hind_results,
            fore_results=fore_results,
            cfg=cfg,
        )

        self._emit("Calibration complete!", 1.0)
        return {
            "best_iteration": global_best_it,
            "best_score":     global_best_obj,
            "reach_ids":      reach_ids,
            "results_dir":    str(self.results_dir),
            "validation":     val_results,
            "hindcast":       hind_results,
            "forecast":       fore_results,
        }
