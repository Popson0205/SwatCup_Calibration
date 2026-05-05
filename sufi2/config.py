"""
sufi2.config
============
Pydantic-based configuration schema for SUFI-2 SWAT calibration.

Users create a YAML file (see `sufi2 init`) and pass it via:
  - CLI:  sufi2 run --config config.yaml
  - API:  POST /run  (multipart: config file + data files)
  - UI:   form fields that map to this schema
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Dict, List, Optional

import yaml
from pydantic import BaseModel, Field, field_validator, model_validator


# ─────────────────────────────────────────────────────────────────────────────
# Sub-models
# ─────────────────────────────────────────────────────────────────────────────

class PeriodConfig(BaseModel):
    """Date range for a simulation period."""
    start: str = Field(..., description="Start date YYYY-MM-DD")
    end:   str = Field(..., description="End date   YYYY-MM-DD")


class CalibrationConfig(BaseModel):
    """Core SUFI-2 calibration settings."""
    iterations:    int   = Field(3,     ge=1, le=50,   description="Number of SUFI-2 iterations")
    n_samples:     int   = Field(500,   ge=10, le=5000, description="LHS samples per iteration")
    obj_func:      str   = Field("NSE", description="Objective function: NSE | KGE | R2")
    top_fraction:  float = Field(0.10,  gt=0.0, lt=1.0, description="Top fraction used to update ranges")
    warmup_years:  int   = Field(3,     ge=0, le=10,   description="SWAT warm-up years (excluded from scoring)")
    seed:          int   = Field(42,    description="Random seed for reproducibility")
    reach_weights: Optional[Dict[int, float]] = Field(
        None,
        description="Per-reach weights for combined objective. None = equal weights. "
                    "Example: {13: 0.2, 15: 0.2, 16: 0.3, 17: 0.3}"
    )

    @field_validator("obj_func")
    @classmethod
    def _valid_obj(cls, v: str) -> str:
        allowed = {"NSE", "KGE", "R2"}
        if v.upper() not in allowed:
            raise ValueError(f"obj_func must be one of {allowed}, got '{v}'")
        return v.upper()


class PathsConfig(BaseModel):
    """File system paths used by the tool."""
    work_dir:    str = Field(".",         description="Directory containing SWAT input files (txtInout)")
    results_dir: str = Field("sufi2_results", description="Output directory (relative to work_dir)")
    swat_exe:    Optional[str] = Field(None, description="Path to SWAT executable. None = auto-detect or mock")
    par_inf:     Optional[str] = Field(None, description="Parameter ranges file. None = built-in defaults")

    @property
    def work_path(self) -> Path:
        return Path(self.work_dir).resolve()

    @property
    def results_path(self) -> Path:
        return self.work_path / self.results_dir


class PeriodsConfig(BaseModel):
    """Simulation period definitions."""
    calibration: PeriodConfig = Field(
        default_factory=lambda: PeriodConfig(start="1993-01-01", end="2001-12-31")
    )
    validation: PeriodConfig = Field(
        default_factory=lambda: PeriodConfig(start="2002-01-01", end="2005-12-31")
    )
    hindcast: Optional[PeriodConfig] = Field(
        None,
        description="Optional hindcast period (simulation only, no scoring)"
    )
    forecast: Optional[PeriodConfig] = Field(
        None,
        description="Optional forecast period (simulation only, no scoring)"
    )


# ─────────────────────────────────────────────────────────────────────────────
# Root config
# ─────────────────────────────────────────────────────────────────────────────

class SUFI2Config(BaseModel):
    """
    Root configuration for a SUFI-2 calibration run.

    Load from YAML:
        cfg = SUFI2Config.from_yaml("config.yaml")

    Scaffold a starter file:
        sufi2 init  (writes config.yaml to current directory)
    """
    paths:       PathsConfig       = Field(default_factory=PathsConfig)
    calibration: CalibrationConfig = Field(default_factory=CalibrationConfig)
    periods:     PeriodsConfig     = Field(default_factory=PeriodsConfig)
    reach_ids:   Optional[List[int]] = Field(
        None,
        description="Reaches to calibrate. None = auto-detect from observed_flow_rchN.csv files"
    )
    backup_inputs: bool = Field(
        True,
        description="Backup original SWAT input files before any modification"
    )

    @model_validator(mode="after")
    def _validate_weights(self) -> "SUFI2Config":
        w = self.calibration.reach_weights
        if w is not None:
            total = sum(w.values())
            if abs(total - 1.0) > 1e-6:
                raise ValueError(
                    f"reach_weights must sum to 1.0, got {total:.4f}"
                )
        return self

    # ── Loaders ──────────────────────────────────────────────────────────────

    @classmethod
    def from_yaml(cls, path: str | Path) -> "SUFI2Config":
        """Load and validate config from a YAML file."""
        p = Path(path)
        if not p.exists():
            raise FileNotFoundError(f"Config file not found: {p}")
        with open(p) as fh:
            data = yaml.safe_load(fh) or {}
        return cls(**data)

    @classmethod
    def from_dict(cls, data: dict) -> "SUFI2Config":
        """Load config from a plain dict (e.g., from API JSON body)."""
        return cls(**data)

    def to_yaml(self, path: str | Path) -> None:
        """Serialize config to YAML."""
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        with open(p, "w") as fh:
            yaml.dump(self.model_dump(), fh, default_flow_style=False, sort_keys=False)


# ─────────────────────────────────────────────────────────────────────────────
# Starter template (written by `sufi2 init`)
# ─────────────────────────────────────────────────────────────────────────────

STARTER_YAML = """\
# ──────────────────────────────────────────────────────────────────────────────
#  SUFI-2 Configuration File
#  Edit the values below, then run:  sufi2 run --config config.yaml
# ──────────────────────────────────────────────────────────────────────────────

paths:
  # Directory that contains your SWAT input files (output.rch, file.cio,
  # observed_flow_rchN.csv, *.mgt, *.gw, *.sol, *.rte, *.hru, *.bsn)
  work_dir: "."

  # Where results will be written (relative to work_dir)
  results_dir: "sufi2_results"

  # Full path to SWAT executable. Leave null to auto-detect (swat2012.exe,
  # swat_rel, swat) or fall back to the built-in mock runner for testing.
  swat_exe: null

  # Parameter ranges file. Leave null to use the built-in 21-parameter defaults.
  par_inf: null

calibration:
  iterations:   3       # Number of SUFI-2 iterations
  n_samples:    500     # LHS samples per iteration
  obj_func:     NSE     # Objective function: NSE | KGE | R2
  top_fraction: 0.10    # Fraction of top simulations used to narrow ranges
  warmup_years: 3       # Warm-up years excluded from objective scoring
  seed:         42      # Random seed (set for reproducibility)

  # Per-reach weights for combined objective. Must sum to 1.0.
  # Leave null for equal weights.
  # reach_weights:
  #   13: 0.25
  #   15: 0.25
  #   16: 0.25
  #   17: 0.25

periods:
  calibration:
    start: "1993-01-01"
    end:   "2001-12-31"
  validation:
    start: "2002-01-01"
    end:   "2005-12-31"

  # Optional: hindcast and forecast periods (simulation only, no scoring)
  # Remove the '#' to enable them.
  # hindcast:
  #   start: "1982-01-01"
  #   end:   "1989-12-31"
  # forecast:
  #   start: "2017-01-01"
  #   end:   "2024-12-31"

  # Example with both enabled:
  # hindcast:
  #   start: "1982-01-01"
  #   end:   "1989-12-31"
  # forecast:
  #   start: "2017-01-01"
  #   end:   "2024-12-31"
  #
  # Note: hindcast/forecast use the best calibrated parameter set.
  # Results are saved as hindcast_rchN.csv / forecast_rchN.csv in results_dir.
  # No objective scoring is applied (no observed data required).

# Reaches to calibrate. Leave null to auto-detect from observed_flow_rchN.csv.
# reach_ids: [13, 15, 16, 17]

# Backup original SWAT input files before modifying them
backup_inputs: true
"""
