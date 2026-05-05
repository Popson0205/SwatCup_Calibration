"""
sufi2.cli
=========
Command-line interface for the SUFI-2 SWAT calibration tool.

Commands
--------
  sufi2 init      — scaffold a starter config.yaml
  sufi2 validate  — validate config + check required files
  sufi2 run       — run calibration
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

import click

from sufi2.config import SUFI2Config, STARTER_YAML


# ─────────────────────────────────────────────────────────────────────────────
# Logging setup
# ─────────────────────────────────────────────────────────────────────────────

def _setup_logging(verbose: bool):
    level = logging.DEBUG if verbose else logging.INFO
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(logging.Formatter("%(asctime)s  %(levelname)-7s  %(message)s", datefmt="%H:%M:%S"))
    logging.basicConfig(level=level, handlers=[handler], force=True)


# ─────────────────────────────────────────────────────────────────────────────
# CLI group
# ─────────────────────────────────────────────────────────────────────────────

@click.group()
@click.version_option(version="1.0.0", prog_name="sufi2")
def cli():
    """
    SUFI-2 SWAT Multi-Reach Calibration Tool

    \b
    Quick start:
        sufi2 init                # scaffold config.yaml
        sufi2 validate            # check files & config
        sufi2 run                 # run calibration
    """


# ─────────────────────────────────────────────────────────────────────────────
# sufi2 init
# ─────────────────────────────────────────────────────────────────────────────

@cli.command()
@click.option(
    "--output", "-o",
    default="config.yaml",
    show_default=True,
    help="Path to write the starter config file.",
)
@click.option("--force", is_flag=True, help="Overwrite existing file.")
def init(output: str, force: bool):
    """Scaffold a starter config.yaml with all options documented."""
    out = Path(output)
    if out.exists() and not force:
        click.echo(f"[ERROR] {out} already exists. Use --force to overwrite.")
        sys.exit(1)
    out.write_text(STARTER_YAML, encoding="utf-8")
    click.echo(f"✓ Config written to: {out.resolve()}")
    click.echo("\nNext steps:")
    click.echo(f"  1. Edit {out} — set work_dir, periods, iterations, etc.")
    click.echo(f"  2. sufi2 validate --config {out}")
    click.echo(f"  3. sufi2 run --config {out}")


# ─────────────────────────────────────────────────────────────────────────────
# sufi2 validate
# ─────────────────────────────────────────────────────────────────────────────

@cli.command()
@click.option(
    "--config", "-c",
    default="config.yaml",
    show_default=True,
    help="Path to config YAML file.",
)
@click.option("--verbose", "-v", is_flag=True)
def validate(config: str, verbose: bool):
    """Validate config file and check that required SWAT files are present."""
    _setup_logging(verbose)
    log = logging.getLogger("sufi2.cli")

    click.echo(f"\n── Validating config: {config}")
    try:
        cfg = SUFI2Config.from_yaml(config)
        click.echo("  ✓ Config schema valid")
    except Exception as e:
        click.echo(f"  ✗ Config error: {e}")
        sys.exit(1)

    work_dir = cfg.paths.work_path
    click.echo(f"\n── Checking work_dir: {work_dir}")
    if not work_dir.exists():
        click.echo(f"  ✗ work_dir does not exist: {work_dir}")
        sys.exit(1)

    issues = []

    # observed flow files
    obs_files = sorted(work_dir.glob("observed_flow_rch*.csv")) or sorted(work_dir.glob("obs_flow_rch*.csv"))
    if obs_files:
        click.echo(f"  ✓ observed flow files: {[f.name for f in obs_files]}")
    else:
        issues.append("No observed_flow_rchN.csv files found")
        click.echo("  ✗ No observed_flow_rchN.csv files found")

    # output.rch
    rch = work_dir / "output.rch"
    if rch.exists():
        click.echo(f"  ✓ output.rch found")
    else:
        issues.append("output.rch not found — run SWAT once to generate it")
        click.echo("  ✗ output.rch not found (run SWAT once to generate it)")

    # file.cio
    cio = work_dir / "file.cio"
    if cio.exists():
        click.echo(f"  ✓ file.cio found")
    else:
        click.echo("  ⚠  file.cio not found (optional)")

    # par_inf
    if cfg.paths.par_inf:
        pf = Path(cfg.paths.par_inf)
        if pf.exists():
            click.echo(f"  ✓ par_inf found: {pf}")
        else:
            issues.append(f"par_inf not found: {pf}")
            click.echo(f"  ✗ par_inf not found: {pf}")
    else:
        click.echo("  ✓ par_inf: using built-in defaults")

    # SWAT exe
    if cfg.paths.swat_exe:
        ep = Path(cfg.paths.swat_exe)
        if ep.exists():
            click.echo(f"  ✓ swat_exe found: {ep}")
        else:
            issues.append(f"swat_exe not found: {ep}")
            click.echo(f"  ✗ swat_exe not found: {ep}")
    else:
        click.echo("  ⚠  swat_exe: not set — will auto-detect or use mock runner")

    click.echo(f"\n── Calibration settings")
    click.echo(f"  Iterations : {cfg.calibration.iterations}")
    click.echo(f"  Samples    : {cfg.calibration.n_samples}")
    click.echo(f"  Objective  : {cfg.calibration.obj_func}")
    click.echo(f"  Calib      : {cfg.periods.calibration.start} → {cfg.periods.calibration.end}")
    click.echo(f"  Validation : {cfg.periods.validation.start} → {cfg.periods.validation.end}")

    if issues:
        click.echo(f"\n✗ Validation failed — {len(issues)} issue(s):")
        for i in issues:
            click.echo(f"    • {i}")
        sys.exit(1)
    else:
        click.echo("\n✓ All checks passed — ready to run!")


# ─────────────────────────────────────────────────────────────────────────────
# sufi2 run
# ─────────────────────────────────────────────────────────────────────────────

@cli.command()
@click.option(
    "--config", "-c",
    default="config.yaml",
    show_default=True,
    help="Path to config YAML file.",
)
@click.option("--verbose", "-v", is_flag=True, help="Enable debug logging.")
def run(config: str, verbose: bool):
    """Run SUFI-2 calibration using the specified config file."""
    _setup_logging(verbose)

    click.echo(f"\n{'='*60}")
    click.echo("  SUFI-2 SWAT Multi-Reach Calibration")
    click.echo(f"  Config: {config}")
    click.echo(f"{'='*60}\n")

    try:
        cfg = SUFI2Config.from_yaml(config)
    except Exception as e:
        click.echo(f"[ERROR] Failed to load config: {e}")
        sys.exit(1)

    from sufi2.core import SUFI2Engine

    def progress(msg: str, pct: float):
        bar_len = 30
        filled  = int(bar_len * pct)
        bar     = "█" * filled + "░" * (bar_len - filled)
        click.echo(f"  [{bar}] {pct*100:5.1f}%  {msg}")

    engine = SUFI2Engine(cfg, progress_callback=progress)

    try:
        result = engine.run()
    except FileNotFoundError as e:
        click.echo(f"\n[ERROR] {e}")
        sys.exit(1)
    except Exception as e:
        click.echo(f"\n[ERROR] Unexpected error: {e}")
        raise

    click.echo(f"\n{'='*60}")
    click.echo("  CALIBRATION COMPLETE")
    click.echo(f"  Best iteration : {result['best_iteration']}")
    click.echo(f"  Best score     : {result['best_score']:.4f}")
    click.echo(f"  Reaches        : {result['reach_ids']}")
    click.echo(f"  Results dir    : {result['results_dir']}")
    click.echo(f"{'='*60}\n")

    if result.get("validation"):
        click.echo("  Validation metrics:")
        for rid, m in result["validation"].items():
            click.echo(f"    Reach {rid:3d} — NSE:{m['NSE']:6.3f}  KGE:{m['KGE']:6.3f}  R²:{m['R2']:5.3f}  PBIAS:{m['PBIAS']:+.1f}%")
        click.echo()


if __name__ == "__main__":
    cli()
