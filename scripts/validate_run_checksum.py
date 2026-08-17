"""
scripts/validate_run_checksum.py
=================================
Default validation step for the Sensitivity & Config Migration Campaign
(docs/BACKLOG.md, "Sensitivity & Config Migration Campaign" section).
Replaces manual visual inspection of a full pipeline run: computes a
content checksum of every scientifically-relevant output file for one
country, comparing a reference snapshot (outputs_baseline/{CODE}_baseline/
-- the existing local-only regression-baseline convention, see
.gitignore's "Local baseline snapshots for regression diffing") against a
current run (outputs/{CODE}/).

Reports MATCH or DIFFER. On DIFFER, prints a per-file summary and, for
any differing result.pkl, a key-path value diff (mirrors the ad-hoc style
of this repo's earlier checkpoint1_pkl_diff_report.txt).

What is hashed, and why
------------------------
  - result.pkl    Loaded via pickle. Known run-bookkeeping keys
                   (timestamp, elapsed_total, timings, ...) and any
                   filesystem-path string are stripped/collapsed to a
                   basename before hashing -- those differ between any
                   two runs by construction (different output roots,
                   different wall-clock time) and carry no scientific
                   content.
  - **/*.csv      Raw file bytes. Deterministic tabular output; no
                   embedded timestamps expected.
  - **/*.tif      Pixel array bytes via rasterio, NOT raw file bytes --
                   avoids false DIFFER from GDAL header/metadata churn
                   that carries no scientific meaning.

Deliberately NOT hashed: *.png (rendering can carry harmless
antialiasing/font-cache noise across environments and are review
artifacts, not scientific results), *.txt reports (timestamped filenames
and content), manifest.json / pipeline_state.json (orchestrator
bookkeeping, not pipeline science).

Usage
-----
    python scripts/validate_run_checksum.py PRT
    python scripts/validate_run_checksum.py PRT \
        --baseline-dir outputs_baseline/PRT_baseline --current-dir outputs/PRT

Exit code 0 on MATCH, 1 on DIFFER, so it can gate a script/CI step.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import pickle
import sys
from pathlib import Path
from typing import Any, Dict, List, Tuple

import numpy as np
import pandas as pd
import rasterio

REPO_ROOT = Path(__file__).resolve().parents[1]

# Run-bookkeeping keys stripped from result.pkl before hashing -- these
# differ between any two runs by construction and are not scientific
# content (see module docstring).
VOLATILE_KEYS = {
    "timestamp", "timestamps", "elapsed_total", "elapsed", "elapsed_s",
    "timings", "started_at", "generated_at", "run_id", "created_at",
}
_PATH_EXTENSIONS = (".tif", ".json", ".txt", ".png", ".pkl", ".csv")


def _strip_volatile(obj: Any) -> Any:
    """Recursively drop bookkeeping keys and collapse path-like strings to
    their basename, so two runs with identical scientific content hash
    identically regardless of when/where they ran."""
    if isinstance(obj, dict):
        return {
            k: _strip_volatile(v)
            for k, v in obj.items()
            if not (isinstance(k, str) and k.lower() in VOLATILE_KEYS)
        }
    if isinstance(obj, list):
        return [_strip_volatile(v) for v in obj]
    if isinstance(obj, str) and obj.endswith(_PATH_EXTENSIONS) and ("/" in obj or "\\" in obj):
        return Path(obj).name
    if isinstance(obj, np.floating):
        return float(obj)
    if isinstance(obj, np.integer):
        return int(obj)
    if isinstance(obj, np.ndarray):
        return obj.tolist()
    return obj


def _canonical_json(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, default=str)


def _hash_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _hash_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def hash_pickle(path: Path) -> Tuple[str, Any]:
    with open(path, "rb") as fh:
        obj = pickle.load(fh)
    stripped = _strip_volatile(obj)
    return _hash_text(_canonical_json(stripped)), stripped


def hash_csv(path: Path) -> str:
    return _hash_bytes(path.read_bytes())


def hash_tif(path: Path) -> str:
    with rasterio.open(str(path)) as src:
        arr = src.read()
    return _hash_bytes(arr.tobytes())


def collect_manifest(root: Path) -> Dict[str, Tuple[str, Any]]:
    """relative_path -> (hash, decoded_value_or_None) for every relevant
    file under *root*. decoded_value is only populated for result.pkl,
    so a DIFFER can print a key-path diff instead of just a hash."""
    manifest: Dict[str, Tuple[str, Any]] = {}
    if not root.exists():
        return manifest

    for pkl_path in sorted(root.rglob("result.pkl")):
        rel = str(pkl_path.relative_to(root)).replace("\\", "/")
        try:
            h, stripped = hash_pickle(pkl_path)
            manifest[rel] = (h, stripped)
        except Exception as exc:
            manifest[rel] = (f"ERROR:{exc}", None)

    for csv_path in sorted(root.rglob("*.csv")):
        rel = str(csv_path.relative_to(root)).replace("\\", "/")
        manifest[rel] = (hash_csv(csv_path), None)

    for tif_path in sorted(root.rglob("*.tif")):
        rel = str(tif_path.relative_to(root)).replace("\\", "/")
        try:
            manifest[rel] = (hash_tif(tif_path), None)
        except Exception as exc:
            manifest[rel] = (f"ERROR:{exc}", None)

    return manifest


def _diff_values(a: Any, b: Any, path: str, out: List[str], max_lines: int = 40) -> None:
    if len(out) >= max_lines:
        return
    if isinstance(a, dict) and isinstance(b, dict):
        for k in sorted(set(a) | set(b)):
            if k not in a:
                out.append(f"{path}.{k}: missing in BASELINE")
            elif k not in b:
                out.append(f"{path}.{k}: missing in CURRENT")
            else:
                _diff_values(a[k], b[k], f"{path}.{k}", out, max_lines)
        return

    # DataFrames (e.g. sensitivity result.pkl's Sobol/OAT tables) don't
    # support a plain `!=` (ambiguous truth value) -- compare structurally.
    if isinstance(a, pd.DataFrame) or isinstance(b, pd.DataFrame):
        same = (
            isinstance(a, pd.DataFrame)
            and isinstance(b, pd.DataFrame)
            and a.equals(b)
        )
        if not same:
            shape_a = getattr(a, "shape", None)
            shape_b = getattr(b, "shape", None)
            out.append(f"{path}: DataFrame differs (shape {shape_a} vs {shape_b})")
        return

    try:
        differs = bool(a != b)
    except Exception:
        differs = repr(a) != repr(b)
    if differs:
        out.append(f"{path}: {a!r} != {b!r}")


def compare(baseline_root: Path, current_root: Path) -> bool:
    base_manifest = collect_manifest(baseline_root)
    curr_manifest = collect_manifest(current_root)

    all_files = sorted(set(base_manifest) | set(curr_manifest))
    differ_files: List[str] = []
    new_files: List[str] = []
    missing_files: List[str] = []

    for rel in all_files:
        in_base, in_curr = rel in base_manifest, rel in curr_manifest
        if in_base and not in_curr:
            missing_files.append(rel)
        elif in_curr and not in_base:
            new_files.append(rel)
        elif base_manifest[rel][0] != curr_manifest[rel][0]:
            differ_files.append(rel)

    ok = not (differ_files or new_files or missing_files)

    print(f"Baseline: {baseline_root}")
    print(f"Current : {current_root}")
    print(f"Files compared: {len(all_files)}")
    print()

    if ok:
        print("RESULT: MATCH")
        return True

    print("RESULT: DIFFER")
    if missing_files:
        print(f"\n  Missing in current run ({len(missing_files)}):")
        for f in missing_files:
            print(f"    - {f}")
    if new_files:
        print(f"\n  New in current run ({len(new_files)}):")
        for f in new_files:
            print(f"    - {f}")
    if differ_files:
        print(f"\n  Content differs ({len(differ_files)}):")
        for f in differ_files:
            print(f"    - {f}")
            if f.endswith("result.pkl"):
                detail: List[str] = []
                _diff_values(base_manifest[f][1], curr_manifest[f][1], "", detail)
                for line in detail:
                    print(f"        {line.lstrip('.')}")
    return False


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("country_code", help="ISO-3166-alpha-3 code, e.g. PRT")
    parser.add_argument("--baseline-dir", default=None)
    parser.add_argument("--current-dir", default=None)
    args = parser.parse_args()

    code = args.country_code.upper()
    baseline_dir = (
        Path(args.baseline_dir) if args.baseline_dir
        else REPO_ROOT / "outputs_baseline" / f"{code}_baseline"
    )
    current_dir = (
        Path(args.current_dir) if args.current_dir
        else REPO_ROOT / "outputs" / code
    )

    ok = compare(baseline_dir, current_dir)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
