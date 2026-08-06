"""
src/visualization/dashboard_panels.py
======================================
Reusable dashboard panel components with consistent GeoWorld styling.

Eliminates duplicated plotting logic in:
  - results_writer._plot_executive_dashboard (7 sub-methods)
  - Future: sensitivity_analyzer dashboard
  - Future: comparison dashboards

Architecture:
  - DashboardPanels class encapsulates all panel types
  - Uses GeoWorldStyler for consistent colors/fonts/spacing
  - Each draw_* method is self-contained and testable
  - Automatic axis styling (spines, ticks, grid)

Panel Types:
  - Potential bars (capacity by scenario, grouped)
  - LCOE distribution (box-whisker with IRENA benchmarks)
  - Supply curves (merit-order with capacity axis)
  - Summary table (formatted text table with color rows)
  - Abatement summary (KPI cards + detail table)
"""

from __future__ import annotations

import logging
from typing import Dict, List, Optional

import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
from matplotlib.ticker import FuncFormatter

from src.core.constants import LCOE_BENCHMARK_USD_MWH, TECH_META, TECH_ORDER
from src.utils.map_styling import GeoWorldStyler

logger = logging.getLogger("geoworld.visualization.dashboard_panels")


class DashboardPanels:
    """
    Reusable dashboard panel components.
    
    Usage:
        styler = GeoWorldStyler(viz_cfg, global_dpi=150)
        panels = DashboardPanels(styler)
        
        fig, axes = plt.subplots(2, 3, figsize=(18, 12))
        panels.draw_potential_bars(axes[0, 0], potential_results)
        panels.draw_lcoe_distribution(axes[0, 1], lcoe_results)
        ...
    """
    
    def __init__(self, styler: GeoWorldStyler):
        """
        Parameters:
            styler: Configured GeoWorldStyler instance for consistent theming
        """
        self.styler = styler
    
    # ═══════════════════════════════════════════════════════════════════
    # Panel 1: Potential Bars (Capacity by Scenario)
    # ═══════════════════════════════════════════════════════════════════
    
    def draw_potential_bars(
        self,
        ax: plt.Axes,
        potential_results: Dict,
        scenarios: Optional[List[str]] = None,
    ) -> None:
        """
        Grouped horizontal bars showing capacity by scenario.
        
        Parameters:
            ax: Matplotlib axes to draw on
            potential_results: Dict from PotentialResult.model_dump()
            scenarios: List of scenario names (default: [optimistic, balanced, conservative])
        """
        if scenarios is None:
            scenarios = ["optimistic", "balanced", "conservative"]
        
        scenario_colors = {
            "optimistic": "#93C5FD",      # blue-300
            "balanced": "#3B82F6",        # blue-600
            "conservative": "#1E3A8A",    # blue-900
        }
        scenario_labels = {
            "optimistic": "Optimistic",
            "balanced": "Balanced",
            "conservative": "Conservative",
        }
        
        bar_width = 0.24
        x_positions = np.arange(len(TECH_ORDER))
        max_val = 0.0
        
        for j, scenario in enumerate(scenarios):
            gw_values = []
            
            for tech in TECH_ORDER:
                sc_data = self._get_scenario_data(
                    potential_results, tech, scenario
                )
                gw = sc_data.get("capacity_gw", 0.0)
                gw_values.append(gw)
            
            max_val = max(max_val, max(gw_values) if gw_values else 0)
            
            bars = ax.bar(
                x_positions + (j - 1) * bar_width,
                gw_values,
                width=bar_width,
                color=scenario_colors.get(scenario, "#3B82F6"),
                label=scenario_labels.get(scenario, scenario.title()),
                edgecolor="white",
                linewidth=0.5,
                alpha=0.92,
            )
            
            # Value labels on bars
            for bar, val in zip(bars, gw_values):
                if val > 1:
                    ax.text(
                        bar.get_x() + bar.get_width() / 2,
                        bar.get_height() + max_val * 0.012,
                        f"{val:.0f}",
                        ha="center", va="bottom",
                        fontsize=5.5, color="#374151",
                    )
        
        ax.set_xticks(x_positions)
        ax.set_xticklabels(
            [TECH_META[t]["short"] for t in TECH_ORDER],
            fontsize=8,
        )
        ax.set_ylabel("Installable Capacity (GW)", fontsize=8)
        ax.set_xlim(-0.5, len(TECH_ORDER) - 0.5)
        
        ax.legend(
            fontsize=7, framealpha=0.9, ncol=3,
            loc="upper right", edgecolor="#E5E7EB",
        )
        
        ax.yaxis.set_major_formatter(
            FuncFormatter(lambda v, _: f"{v:.0f}")
        )
        
        self._apply_clean_style(ax)
    
    # ═══════════════════════════════════════════════════════════════════
    # Panel 2: LCOE Distribution (Box-Whisker)
    # ═══════════════════════════════════════════════════════════════════
    
    def draw_lcoe_distribution(
        self,
        ax: plt.Axes,
        lcoe_results: Dict,
    ) -> None:
        """
        Horizontal box-whisker plot with IRENA benchmark lines.
        
        Requires stats keys: p10, p25, median, p75, p90
        (guaranteed by data_recovery.recover_lcoe_from_disk)
        
        Parameters:
            ax: Matplotlib axes
            lcoe_results: Dict from LCOEResult.model_dump()
        """
        y_positions = [2, 1, 0]  # Reversed order (solar on top)
        
        for tech, yp in zip(TECH_ORDER, y_positions):
            tech_data = lcoe_results.get("techs", {}).get(tech, {})
            stats = tech_data.get("stats", {})
            lp = TECH_META[tech]
            
            if not stats:
                continue
            
            p10 = stats.get("p10", 0.0)
            p25 = stats.get("p25", 0.0)
            median = stats.get("median", 0.0)
            p75 = stats.get("p75", 0.0)
            p90 = stats.get("p90", 0.0)
            
            # Whisker line p10–p90
            ax.plot(
                [p10, p90], [yp, yp],
                color=lp["color"], linewidth=2.0,
                solid_capstyle="round", zorder=4,
            )
            
            # IQR box p25–p75
            ax.barh(
                yp, p75 - p25, left=p25, height=0.38,
                color=lp["color"], alpha=0.35,
                edgecolor=lp["color"], linewidth=0.9,
            )
            
            # Median marker
            ax.scatter(
                median, yp, color=lp["color"], s=60,
                zorder=5, edgecolors="white", linewidths=0.8,
            )
            
            # IRENA benchmark vertical line
            bench = LCOE_BENCHMARK_USD_MWH.get(tech, {})
            if bench.get("median"):
                ax.axvline(
                    bench["median"],
                    color=lp["color"], linewidth=0.9,
                    linestyle=":", alpha=0.45, zorder=3,
                )
        
        ax.set_yticks(y_positions)
        ax.set_yticklabels(
            [TECH_META[t]["short"] for t in reversed(TECH_ORDER)],
            fontsize=8,
        )
        ax.set_xlabel("LCOE ($/MWh)", fontsize=8)
        ax.set_ylim(-0.5, 2.5)
        
        # Market reference line
        ax.axvline(
            75, color="#374151", linewidth=0.9,
            linestyle="--", alpha=0.55, label="Market ~75 $/MWh",
        )
        
        ax.legend(fontsize=7, framealpha=0.9, edgecolor="#E5E7EB")
        self._apply_clean_style(ax)
    
    # ═══════════════════════════════════════════════════════════════════
    # Panel 3: Supply Curves (Merit Order)
    # ═══════════════════════════════════════════════════════════════════
    
    def draw_supply_curves(
        self,
        ax: plt.Axes,
        lcoe_results: Dict,
    ) -> None:
        """
        Merit-order supply curves with shaded area under curves.
        
        Uses supply_curve DataFrame when available.
        Falls back gracefully if not present.
        
        Parameters:
            ax: Matplotlib axes
            lcoe_results: Dict from LCOEResult.model_dump()
        """
        plotted = False
        
        for tech in TECH_ORDER:
            tech_data = lcoe_results.get("techs", {}).get(tech, {})
            supply_curve = tech_data.get("supply_curve")
            lp = TECH_META[tech]
            
            if supply_curve is None or not isinstance(supply_curve, pd.DataFrame):
                continue
            
            if supply_curve.empty:
                continue
            
            # Identify LCOE column
            lcoe_col = next(
                (c for c in ["lcoe_usd_mwh", "lcoe_eur_mwh"] if c in supply_curve.columns),
                None,
            )
            
            if lcoe_col is None or "cum_capacity_gw" not in supply_curve.columns:
                continue
            
            # Down-sample for performance (max 2000 points)
            df = supply_curve
            if len(df) > 2000:
                idx = np.linspace(0, len(df) - 1, 2000, dtype=int)
                df = df.iloc[idx].reset_index(drop=True)
            
            # Plot curve with shaded area
            ax.fill_between(
                df["cum_capacity_gw"],
                df[lcoe_col],
                alpha=0.10,
                color=lp["color"],
            )
            ax.plot(
                df["cum_capacity_gw"],
                df[lcoe_col],
                color=lp["color"],
                linewidth=1.8,
                label=lp["short"],
            )
            
            # IRENA benchmark horizontal line
            bench = LCOE_BENCHMARK_USD_MWH.get(tech, {})
            if bench.get("median"):
                ax.axhline(
                    bench["median"],
                    color=lp["color"],
                    linewidth=0.8,
                    linestyle="--",
                    alpha=0.40,
                )
            
            plotted = True
        
        if not plotted:
            # No supply curve data available
            ax.text(
                0.5, 0.5,
                "Supply curve data unavailable\n(requires Phase 5 serialization)",
                ha="center", va="center",
                fontsize=9, style="italic", color="#6B7280",
                transform=ax.transAxes,
            )
            ax.axis("off")
            return
        
        # Market reference line
        ax.axhline(
            75, color="#374151", linewidth=0.9,
            linestyle=":", alpha=0.55, label="~75 $/MWh",
        )
        
        ax.set_xlabel("Cumulative Capacity (GW)", fontsize=8)
        ax.set_ylabel("LCOE ($/MWh)", fontsize=8)
        ax.set_xlim(left=0)
        
        ax.legend(
            fontsize=7, framealpha=0.9,
            edgecolor="#E5E7EB", loc="upper left",
        )
        
        ax.grid(axis="y", alpha=0.18, linestyle="--")
        self._apply_clean_style(ax)
    
    # ═══════════════════════════════════════════════════════════════════
    # Panel 4: Summary Table
    # ═══════════════════════════════════════════════════════════════════
    
    def draw_summary_table(
        self,
        ax: plt.Axes,
        potential_results: Dict,
        lcoe_results: Dict,
        scenario: str = "balanced",
    ) -> None:
        """
        Formatted text table with key metrics.
        
        Parameters:
            ax: Matplotlib axes (will be set to axis("off"))
            potential_results: Dict from PotentialResult.model_dump()
            lcoe_results: Dict from LCOEResult.model_dump()
            scenario: Scenario name to display (default: "balanced")
        """
        ax.axis("off")
        
        headers = [
            "Technology",
            "Capacity\n(GW)",
            "Generation\n(TWh/yr)",
            "Mean LCOE\n($/MWh)",
            "P10 LCOE\n($/MWh)",
        ]
        
        rows: List[List[str]] = []
        total_gw = 0.0
        total_twh = 0.0
        
        for tech in TECH_ORDER:
            sc_data = self._get_scenario_data(
                potential_results, tech, scenario
            )
            stats = (
                lcoe_results.get("techs", {})
                .get(tech, {})
                .get("stats", {})
            )
            
            gw = sc_data.get("capacity_gw", 0.0)
            twh = sc_data.get("generation_twh", 0.0)
            mean_lcoe = stats.get("mean", 0.0)
            p10_lcoe = stats.get("p10", 0.0)
            
            total_gw += gw
            total_twh += twh
            
            rows.append([
                TECH_META[tech]["label"],
                f"{gw:.1f}",
                f"{twh:.1f}",
                f"{mean_lcoe:.1f}",
                f"{p10_lcoe:.1f}",
            ])
        
        # Total row
        rows.append([
            "TOTAL",
            f"{total_gw:.1f}",
            f"{total_twh:.1f}",
            "—",
            "—",
        ])
        
        # Create table
        table = ax.table(
            cellText=rows,
            colLabels=headers,
            cellLoc="center",
            loc="center",
            bbox=[0, 0, 1, 1],
        )
        
        table.auto_set_font_size(False)
        table.set_fontsize(7.5)
        
        # Header styling
        for j in range(len(headers)):
            cell = table[0, j]
            cell.set_facecolor("#1E3A8A")  # blue-900
            cell.set_text_props(color="white", fontweight="bold")
        
        # Row colors
        row_colors = [
            "#FEF9C3",  # yellow-100 (solar)
            "#DBEAFE",  # blue-100 (wind)
            "#DCFCE7",  # green-100 (biomass)
            "#F3F4F6",  # gray-100 (total)
        ]
        
        for i, bg_color in enumerate(row_colors):
            for j in range(len(headers)):
                cell = table[i + 1, j]
                cell.set_facecolor(bg_color)
                # Bold total row
                if i == 3:
                    cell.set_text_props(fontweight="bold")
    
    # ═══════════════════════════════════════════════════════════════════
    # Panel 5: Abatement Summary (Optional)
    # ═══════════════════════════════════════════════════════════════════
    
    def draw_abatement_summary(
        self,
        ax: plt.Axes,
        abatement_results: Dict,
    ) -> None:
        """
        GHG abatement summary with KPI cards and detail table.
        
        Parameters:
            ax: Matplotlib axes
            abatement_results: Dict from recover_abatement_from_disk()
        """
        ax.axis("off")
        ax.set_xlim(0, 1)
        ax.set_ylim(0, 1)
        
        if not abatement_results.get("available", False):
            ax.text(
                0.5, 0.5,
                "GHG Abatement data unavailable\n(requires Phase 7)",
                ha="center", va="center",
                fontsize=10, style="italic", color="#6B7280",
            )
            return
        
        # Extract metrics
        subst_twh = abatement_results.get("subst_gwh", 0) / 1000
        co2_mt = abatement_results.get("co2_avoided_mt", 0)
        val_b = abatement_results.get("total_value_b", 0)
        mac = abatement_results.get("mac_usd_tco2e", abatement_results.get("mac_global", 0))
        cp = abatement_results.get("carbon_price", 80)
        capex_b = abatement_results.get("capex_total_b", 0)
        ndc_cov = abatement_results.get("ndc_coverage_pct", 0)
        ci_before = abatement_results.get("ci_before", 0)
        ci_after = abatement_results.get("ci_after", 0)
        net_av = abatement_results.get("net_avoided_mt", co2_mt)
        
        # ── Title ──────────────────────────────────────────────────────
        ax.text(
            0.5, 0.97,
            "⚡ GHG Abatement — Electricity Sector",
            ha="center", va="top",
            fontsize=9, fontweight="bold", color="#111827",
        )
        ax.axhline(0.91, color="#D1D5DB", linewidth=0.8)
        
        # ── KPI Cards ──────────────────────────────────────────────────
        kpis = [
            (f"{co2_mt:.2f}", "MtCO₂e/yr\nAvoided (gross)", "#991b1b", 0.15),
            (f"${val_b:.2f}B", "USD/yr\nEcon. Value", "#15803d", 0.50),
            (f"{subst_twh:.1f}", "TWh/yr\nSubstituted", "#1e40af", 0.85),
        ]
        
        for val_str, label, color, x_pos in kpis:
            ax.text(
                x_pos, 0.82, val_str,
                ha="center", fontsize=22,
                fontweight="bold", color=color,
            )
            ax.text(
                x_pos, 0.70, label,
                ha="center", fontsize=8,
                style="italic", color="#374151",
            )
        
        ax.axhline(0.64, color="#D1D5DB", linewidth=0.8)
        
        # ── Detail Table ───────────────────────────────────────────────
        mac_label = "Self-financing" if mac <= 0 else f"MAC ${mac:.1f}/tCO₂e"
        
        details = [
            ("MAC", mac_label),
            ("Est. CAPEX", f"${capex_b:.2f} B USD"),
            ("Carbon price", f"${cp:.0f}/tCO₂e"),
            ("Net avoided", f"{net_av:.2f} MtCO₂e/yr"),
            ("Carbon intensity", f"{ci_before:.0f}→{ci_after:.0f} gCO₂/kWh"),
            ("NDC 2030", f"{ndc_cov:.0f}% of gap"),
        ]
        
        y_start = 0.57
        for i, (label, value) in enumerate(details):
            row_y = y_start - i * 0.085
            
            ax.text(
                0.04, row_y, f"{label}:",
                ha="left", fontsize=8, color="#6B7280",
            )
            ax.text(
                0.96, row_y, value,
                ha="right", fontsize=8,
                fontweight="bold", color="#111827",
            )
        
        ax.axhline(0.05, color="#D1D5DB", linewidth=0.8)
        
        # ── MAC Assessment Badge ───────────────────────────────────────
        if mac <= 0:
            mac_color = "#15803d"  # green-700
            mac_note = "✅ Self-financing"
        elif cp >= mac:
            mac_color = "#15803d"
            mac_note = "✅ Carbon price covers MAC"
        else:
            mac_color = "#991b1b"  # red-800
            mac_note = f"⚠️  Gap ${mac - cp:.1f}/tCO₂e to breakeven"
        
        ax.text(
            0.5, 0.025, mac_note,
            ha="center", fontsize=7.5, color=mac_color,
            bbox=dict(
                boxstyle="round,pad=0.3",
                facecolor="#F9FAFB",
                edgecolor="#D1D5DB",
                alpha=0.9,
            ),
        )
    
    # ═══════════════════════════════════════════════════════════════════
    # Internal Helpers
    # ═══════════════════════════════════════════════════════════════════
    
    @staticmethod
    def _get_scenario_data(
        potential_results: Dict,
        tech: str,
        scenario: str,
    ) -> Dict:
        """
        Extract scenario dict from potential_results.
        
        Handles both live run format and disk recovery format.
        """
        # Primary path: {"techs": {"solar": {"scenarios": {"balanced": {...}}}}}
        sc = (
            potential_results
            .get("techs", {})
            .get(tech, {})
            .get("scenarios", {})
            .get(scenario, {})
        )
        if sc:
            return sc
        
        # Legacy flat path: {"solar": {"scenarios": {"balanced": {...}}}}
        sc = (
            potential_results
            .get(tech, {})
            .get("scenarios", {})
            .get(scenario, {})
        )
        return sc or {}
    
    def _apply_clean_style(self, ax: plt.Axes) -> None:
        """Apply consistent clean styling to axis (spines, ticks, grid)."""
        ax.spines[["top", "right"]].set_visible(False)
        ax.tick_params(labelsize=7)
        ax.grid(False)