"""
src/utils/reporting.py
======================
Universal text report builder for all GeoWorld pipeline phases.

Eliminates duplicated formatting logic across:
  - suitability_builder
  - sensitivity_analyzer
  - lcoe_calculator
  - potential_calculator
  - results_writer

Architecture:
  - ReportSection: hierarchical data structure
  - build_phase_report: unified formatter with standard blocks
  - Consistent width, separators, alignment across all phases
"""

from __future__ import annotations

from datetime import datetime  
from pathlib import Path       
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

import numpy as np

from src.core.constants import NODATA_FLOAT

logger = __import__("logging").getLogger("geoworld.utils.reporting")


@dataclass
class ReportSection:
    """
    Hierarchical section for structured text reports.
    
    Examples:
        # Simple section
        ReportSection(
            title="SUITABILITY DOMINANCE",
            rows=[
                ("Solar PV", "45,231 px (34.2%)"),
                ("Wind Onshore", "67,890 px (51.3%)"),
            ],
        )
        
        # Nested section
        ReportSection(
            title="BALANCED SCENARIO",
            rows=[("Header", "GW | TWh/yr | km² | $/MWh")],
            subsections=[
                ReportSection(
                    title="Solar PV",
                    rows=[("Capacity", "12.5 GW"), ("LCOE", "45.2 $/MWh")],
                ),
            ],
        )
    """
    title: str
    rows: List[Tuple[str, str]] = field(default_factory=list)
    subsections: List["ReportSection"] = field(default_factory=list)
    separator_char: str = "─"  # U+2500 Box Drawings Light Horizontal


def build_phase_report(
    title: str,
    country_name: str,
    country_code: str,
    timestamp: str,
    sections: List[ReportSection],
    timings: Optional[Dict[str, float]] = None,
    elapsed_total: Optional[float] = None,
    width: int = 72,
) -> str:
    """
    Build a formatted text report with consistent styling.
    
    Args:
        title: Main report title
        country_name: Full country name
        country_code: ISO 3166-1 alpha-3 code
        timestamp: ISO timestamp string
        sections: List of ReportSection objects
        timings: Dict of step → elapsed seconds
        elapsed_total: Total elapsed time (seconds)
        width: Report width in characters
    
    Returns:
        Formatted report string
    """
    lines: List[str] = []
    
    def sep(char: str = "═"):
        lines.append(char * width)
    
    # ── Header ─────────────────────────────────────────────────────────
    sep()
    lines.append(f"  {title}")
    lines.append(f"  {country_name} ({country_code})")
    lines.append(f"  {timestamp[:19].replace('T', ' ')}")
    sep()
    lines.append("")
    
    # ── Sections ───────────────────────────────────────────────────────
    for section in sections:
        sep("─")
        lines.append(f"  {section.title}")
        sep("─")
        
        for row in section.rows:
            if isinstance(row, tuple):
                if len(row) == 2:
                    # Two-column layout: (label, value)
                    label, value = row
                    # Detecta se é um header/separator ou linha de dados
                    if label.strip().startswith("─") or not value:
                        # Header ou separator — sem formatação adicional
                        lines.append(f"  {label}")
                    else:
                        # Linha normal — formato compacto
                        lines.append(f"  {label:<40} {value}")
                else:
                    # Multi-column (fallback genérico)
                    lines.append("  " + "  ".join(str(c) for c in row))
            else:
                # String simples
                lines.append(f"  {row}")
        
        lines.append("")
    
    # ── Timings ────────────────────────────────────────────────────────
    if timings:
        sep()
        lines.append("  TIMINGS")
        sep("─")
        
        for step, t in timings.items():
            # Formato: "    <step>:<spacing>: <time>s"
            # Mantém alinhamento compacto (32 chars para label)
            lines.append(f"    {step:<32}: {t:>6.1f}s")
        
        if elapsed_total is not None:
            lines.append(f"    {'TOTAL':<32}: {elapsed_total:>6.1f}s")
        
        sep()
        lines.append("")
    
    return "\n".join(lines)


def _append_section(
    lines: List[str],
    section: ReportSection,
    width: int,
    indent: int,
) -> None:
    """Recursively append a section and its subsections to lines."""
    prefix = "  " * indent
    sep = section.separator_char * (width - len(prefix) - 2)
    
    # Section header
    lines.extend([
        f"{prefix}{sep}",
        f"{prefix}  {section.title}",
        f"{prefix}{sep}",
    ])
    
    # Section rows
    for label, value in section.rows:
        label_col = f"{prefix}  {label}"
        # Right-align value within remaining width
        available = width - len(label_col) - 2
        lines.append(f"{label_col}{value:>{available}}")
    
    # Subsections (indented)
    for subsection in section.subsections:
        lines.append("")
        _append_section(lines, subsection, width, indent + 1)


def _format_timestamp(ts: str) -> str:
    """
    Format ISO timestamp for display.
    
    Converts "2024-01-15T10:30:45.123456" → "2024-01-15 10:30:45"
    """
    if "T" in ts:
        return ts[:19].replace("T", " ")
    return ts[:19] if len(ts) > 19 else ts


# ═══════════════════════════════════════════════════════════════════════
# Convenience Builders for Common Patterns
# ═══════════════════════════════════════════════════════════════════════

def build_technology_section(
    title: str,
    tech_data: Dict[str, Dict[str, float]],
    tech_order: List[str],
    tech_labels: Dict[str, str],
    value_formatters: Dict[str, str],
) -> ReportSection:
    """
    Build a section with one row per technology.
    
    Parameters:
        title: Section title
        tech_data: Dict[tech → Dict[metric → value]]
        tech_order: Ordered list of technology keys
        tech_labels: Dict[tech → display label]
        value_formatters: Dict[metric → format string] (e.g. "{:.1f} GW")
    
    Example:
        tech_data = {
            "solar": {"capacity_gw": 12.5, "lcoe": 45.2},
            "wind":  {"capacity_gw": 8.3,  "lcoe": 52.1},
        }
        value_formatters = {
            "capacity_gw": "{:.1f} GW",
            "lcoe": "{:.1f} $/MWh",
        }
        
        Returns section with rows like:
          "Solar PV                  12.5 GW | 45.2 $/MWh"
    """
    rows: List[Tuple[str, str]] = []
    
    for tech in tech_order:
        if tech not in tech_data:
            continue
        
        label = tech_labels.get(tech, tech)
        metrics = tech_data[tech]
        
        # Build value string from all metrics
        value_parts = []
        for metric, fmt in value_formatters.items():
            if metric in metrics:
                value_parts.append(fmt.format(metrics[metric]))
        
        value_str = " | ".join(value_parts)
        rows.append((label, value_str))
    
    return ReportSection(title=title, rows=rows)


def build_stats_table_section(
    title: str,
    headers: List[str],
    data_rows: List[List[str]],
    total_row: Optional[List[str]] = None,
) -> ReportSection:
    """
    Build a tabular section with aligned columns.
    
    Parameters:
        title: Section title
        headers: Column headers
        data_rows: List of row data (each row is list of strings)
        total_row: Optional footer row (e.g. "TOTAL")
    
    Example:
        headers = ["Technology", "GW", "TWh/yr", "$/MWh"]
        data_rows = [
            ["Solar PV", "12.5", "18.3", "45.2"],
            ["Wind Onshore", "8.3", "15.1", "52.1"],
        ]
        total_row = ["TOTAL", "20.8", "33.4", "—"]
    """
    # Compute column widths
    all_rows = [headers] + data_rows
    if total_row:
        all_rows.append(total_row)
    
    col_widths = [
        max(len(row[i]) for row in all_rows if i < len(row))
        for i in range(len(headers))
    ]
    
    # Format header
    header_str = "  ".join(
        h.ljust(w) for h, w in zip(headers, col_widths)
    )
    
    rows: List[Tuple[str, str]] = [
        (header_str, ""),  # Header as a single "label" (no value column)
    ]
    
    # Data rows
    for row_data in data_rows:
        row_str = "  ".join(
            row_data[i].ljust(col_widths[i]) if i < len(row_data) else ""
            for i in range(len(headers))
        )
        rows.append((row_str, ""))
    
    # Total row (with separator)
    if total_row:
        sep = "─" * sum(col_widths) + "─" * (2 * (len(col_widths) - 1))
        rows.append((sep, ""))
        
        total_str = "  ".join(
            total_row[i].ljust(col_widths[i]) if i < len(total_row) else ""
            for i in range(len(headers))
        )
        rows.append((total_str, ""))
    
    return ReportSection(title=title, rows=rows)


# ═══════════════════════════════════════════════════════════════════════
# Criteria Summary Report (moved from criteria_builder.py)
# ═══════════════════════════════════════════════════════════════════════

def write_criteria_summary(
    criteria: Dict[str, np.ndarray],
    country_code: str,
    country_name: str,
    report_dir: Path,
) -> Path:
    """
    Write a statistical summary of all computed criteria to disk.

    Called by CriteriaBuilder after Phase 2b computation is complete.

    Args:
        criteria:     Mapping criterion_name -> score array.
        country_code: ISO-3 country code.
        country_name: Full country name.
        report_dir:   Output directory for the report file.

    Returns:
        Path to the generated report file.
    """
    report_dir = Path(report_dir)
    report_dir.mkdir(parents=True, exist_ok=True)
    out = report_dir / f"criteria_summary_{country_code}.txt"

    lines = [
        "=" * 60 + "\n",
        f"  CRITERIA SUMMARY — {country_name} ({country_code})\n",
        f"  {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n",
        "=" * 60 + "\n\n",
    ]

    for name, score in criteria.items():
        valid = score[
            np.isfinite(score) & (score != NODATA_FLOAT) & (score >= 0)
        ]
        if not len(valid):
            lines.append(f"  {name}: no data\n\n")
            continue

        bins   = [0, 0.2, 0.4, 0.6, 0.8, 1.001]
        counts = np.histogram(valid, bins=bins)[0]
        total  = len(valid)
        p10, p50, p90 = np.percentile(valid, [10, 50, 90])

        lines.append(
            f"  {name}\n"
            f"    Valid pixels: {total:>10,}\n"
            f"    Mean±Std    : {valid.mean():.4f} ± {valid.std():.4f}\n"
            f"    P10/P50/P90 : {p10:.3f} / {p50:.3f} / {p90:.3f}\n"
            "    Range       |    Count    |     %\n"
            "    " + "-" * 38 + "\n"
        )
        for lbl, cnt in zip(
            ["0.0–0.2", "0.2–0.4", "0.4–0.6", "0.6–0.8", "0.8–1.0"], counts
        ):
            lines.append(
                f"    {lbl:<13} | {cnt:>10,} | {100 * cnt / total:>5.1f}%\n"
            )
        lines.append(f"    Top 10%     : score > {p90:.3f}\n\n")

    out.write_text("".join(lines), encoding="utf-8")
    return out


# ═══════════════════════════════════════════════════════════════════════
# Example Usage (for documentation / testing)
# ═══════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    # Example: Results synthesis report
    
    sections = [
        ReportSection(
            title="SUITABILITY DOMINANCE — TOPSIS Score",
            rows=[
                ("Solar PV", "45,231 px  (34.2%)"),
                ("Wind Onshore", "67,890 px  (51.3%)"),
                ("Biomass / Bioenergy", "19,127 px  (14.5%)"),
            ],
        ),
        
        build_stats_table_section(
            title="BALANCED SCENARIO — INTEGRATED SUMMARY",
            headers=["Technology", "GW", "TWh/yr", "km²", "$/MWh"],
            data_rows=[
                ["Solar PV", "12.5", "18.3", "415", "45.2"],
                ["Wind Onshore", "8.3", "15.1", "830", "52.1"],
                ["Biomass / Bioenergy", "2.1", "9.8", "1,250", "68.5"],
            ],
            total_row=["TOTAL", "22.9", "43.2", "—", "—"],
        ),
        
        ReportSection(
            title="EXPORTED FILES",
            rows=[
                ("✓", "PRT_dominance_suitability.tif"),
                ("✓", "PRT_dominance_lcoe.tif"),
                ("✓", "PRT_executive_dashboard.png"),
            ],
        ),
    ]
    
    report = build_phase_report(
        title="RESULTS SYNTHESIS REPORT — Phase 6",
        country_name="Portugal",
        country_code="PRT",
        timestamp="2024-01-15T10:30:45",
        sections=sections,
        timings={
            "dominance_suitability": 2.3,
            "dominance_lcoe": 1.8,
            "executive_dashboard": 5.1,
            "geotiff_export": 0.9,
        },
        elapsed_total=10.1,
    )
    
    print(report)