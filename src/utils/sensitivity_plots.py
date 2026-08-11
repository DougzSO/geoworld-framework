"""
src/utils/sensitivity_plots.py
===============================
Visualizations for Phase 8 (Sensitivity Analysis).

Separated from SensitivityAnalyzer to keep the main module lean, mirroring
the pattern already established by abatement_plots.py. Each function
receives a GeoWorldStyler and the necessary data explicitly — no global
state, no dependency on the caller's instance attributes.
"""
from __future__ import annotations

import logging
from datetime import date
from pathlib import Path
from typing import Any, Dict, List, Tuple

import matplotlib.gridspec as gridspec
import matplotlib.patches as mpatches
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

from src.utils.map_styling import GeoWorldStyler

logger = logging.getLogger("geoworld.utils.sensitivity_plots")

# ── Visual palette ──────────────────────────────────────────────────────────
TECH_COLOR = {
    "solar":   "#F9A825",
    "wind":    "#1565C0",
    "biomass": "#2E7D32",
}
TECH_LABEL = {
    "solar":   "Solar PV",
    "wind":    "Wind Onshore",
    "biomass": "Biomass / Bioenergy",
}
_GRID_C = "#E5E7EB"
_TEXT_C = "#111827"
_MUTE_C = "#6B7280"


# ── Shared helpers ────────────────────────────────────────────────────────────

def _watermark(fig: plt.Figure) -> None:
    """Add GeoWorld watermark to figure."""
    fig.text(
        0.99, 0.005,
        f"GeoWorld Framework · {date.today()}",
        ha="right", fontsize=6.5, color=_MUTE_C, style="italic",
    )


def _draw_kpis(ax: plt.Axes, kpis: List[Tuple[str, str]]) -> None:
    """Draw KPI summary panel."""
    ax.set_xlim(0, 1)
    ax.set_ylim(0, 1)
    ax.add_patch(mpatches.FancyBboxPatch(
        (0.01, 0.01), 0.98, 0.97,
        boxstyle="round,pad=0.02",
        facecolor="white", edgecolor="#D1D5DB", lw=1,
    ))
    y = 0.92
    for lbl, val in kpis:
        ax.text(0.04, y, lbl + ":", fontsize=7.5, va="top", color=_MUTE_C)
        ax.text(0.96, y, val, fontsize=7.5, va="top", ha="right", color=_TEXT_C, fontweight="bold")
        y -= 0.11
        if y < 0.04:
            break


# ── Figure 1: SA-1 (OAT weight sensitivity) ───────────────────────────────────

def plot_sa1_tornado(styler: GeoWorldStyler, df: pd.DataFrame, tech: str, out_path: Path) -> None:
    """Generate SA-1 tornado plot."""
    summary = (
        df.groupby("criterion")
        .agg(rho_min=("spearman_rho", "min"), weight=("weight_base", "first"))
        .reset_index()
        .sort_values("rho_min")
    )
    fig, ax = plt.subplots(figsize=(10, max(4, len(summary) * 0.48 + 1.6)), dpi=styler.dpi)
    fig.patch.set_facecolor(styler.fig_bg)
    ax.set_facecolor(styler.fig_bg)
    for idx, (_, row) in enumerate(summary.iterrows()):
        r     = row["rho_min"]
        color = "#16a34a" if r >= 0.95 else "#d97706" if r >= 0.90 else "#dc2626"
        ax.barh(idx, 1.0 - r, 0.60, color=color, alpha=0.82)
        ax.text(
            1.0 - r + 0.002, idx,
            f"ρ={r:.3f} (w={row['weight']:.3f})",
            va="center", fontsize=8.5, color=_TEXT_C,
        )
    ax.set_yticks(range(len(summary)))
    ax.set_yticklabels(summary["criterion"], fontsize=9)
    ax.set_xlabel("1 − ρ_min (higher = more sensitive)", fontsize=10)
    ax.axvline(0.05, color="#dc2626", ls="--")
    ax.axvline(0.10, color="#d97706", ls=":")
    ax.spines[["top", "right"]].set_visible(False)
    ax.grid(axis="x", color=_GRID_C, lw=0.7)
    ax.set_title(
        f"SA-1 · OAT Weight Sensitivity — {TECH_LABEL.get(tech, tech)}",
        fontsize=12, fontweight="bold", pad=10,
    )
    _watermark(fig)
    styler.save(fig, out_path)


def plot_sa1_heatmap(styler: GeoWorldStyler, df: pd.DataFrame, tech: str, out_path: Path) -> None:
    """Generate SA-1 heatmap."""
    pivot = df.pivot_table(
        index="criterion", columns="perturbation_pct",
        values="spearman_rho", aggfunc="mean",
    )
    pivot = pivot.loc[pivot.min(axis=1).sort_values().index]
    fig, ax = plt.subplots(figsize=(12, max(4, len(pivot) * 0.45 + 1.5)), dpi=styler.dpi)
    fig.patch.set_facecolor(styler.fig_bg)
    im   = ax.imshow(pivot.values, cmap="RdYlGn", vmin=0.85, vmax=1.0, aspect="auto")
    cbar = fig.colorbar(im, ax=ax, shrink=0.75, pad=0.02)
    cbar.set_label("Spearman ρ", fontsize=9)
    ax.set_xticks(range(len(pivot.columns)))
    ax.set_xticklabels([f"{int(v):+d}%" for v in pivot.columns], fontsize=9)
    ax.set_yticks(range(len(pivot.index)))
    ax.set_yticklabels(pivot.index, fontsize=9)
    for i in range(len(pivot.index)):
        for j in range(len(pivot.columns)):
            v = pivot.values[i, j]
            ax.text(j, i, f"{v:.3f}", ha="center", va="center",
                    fontsize=7, color="black" if v > 0.92 else "white")
    ax.tick_params(top=True, bottom=False, labelbottom=False)
    ax.set_title(
        f"SA-1 · Spearman ρ Heatmap — {TECH_LABEL.get(tech, tech)}",
        fontsize=12, fontweight="bold", pad=10,
    )
    _watermark(fig)
    styler.save(fig, out_path)


# ── Figure 2: SA-2 (Monte Carlo AHP uncertainty) ──────────────────────────────

def plot_sa2_cv(
    styler: GeoWorldStyler, cv: np.ndarray, ci: np.ndarray, tech: str, out_path: Path
) -> None:
    """Generate SA-2 CV distribution plots."""
    color = TECH_COLOR.get(tech, "#2563EB")
    fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(12, 4.8), dpi=styler.dpi)
    fig.patch.set_facecolor(styler.fig_bg)
    for ax, data, title, xlabel, ref_val, ref_lbl in [
        (ax1, cv[np.isfinite(cv) & (cv >= 0)], "Coefficient of Variation (CV)",
         "CV per pixel",       0.25, "CV=0.25"),
        (ax2, ci[np.isfinite(ci) & (ci >= 0)], "90% CI Width",
         "P95 − P05 per pixel", 0.10, "CI90=0.10"),
    ]:
        ax.set_facecolor(styler.fig_bg)
        if data.size > 0:
            ax.hist(data, bins=60, color=color, alpha=0.75, edgecolor="white", lw=0.4)
            med = float(np.median(data))
            p90 = float(np.percentile(data, 90))
            ax.axvline(med,     color="#111827", ls="--", label=f"Median={med:.3f}")
            ax.axvline(p90,     color="#dc2626", ls=":",  label=f"P90={p90:.3f}")
            ax.axvline(ref_val, color="#d97706", ls="-.", alpha=0.7, label=ref_lbl)
            ax.legend(fontsize=8.5)
        ax.set_title(title,  fontsize=10, fontweight="bold")
        ax.set_xlabel(xlabel, fontsize=9)
        ax.spines[["top", "right"]].set_visible(False)
        ax.grid(axis="y", color=_GRID_C, lw=0.7)
    fig.suptitle(
        f"SA-2 · MC AHP Uncertainty — {TECH_LABEL.get(tech, tech)}",
        fontsize=12, fontweight="bold", y=1.02,
    )
    _watermark(fig)
    fig.tight_layout()
    styler.save(fig, out_path)


# ── Figure 3: SA-3 (threshold sweep) ──────────────────────────────────────────

def plot_sa3_threshold(
    styler: GeoWorldStyler, df: pd.DataFrame, tech: str, base_thr: float, out_path: Path
) -> None:
    """Generate SA-3 threshold sweep plot."""
    color = TECH_COLOR.get(tech, "#2563EB")
    fig, ax1 = plt.subplots(figsize=(10, 5.5), dpi=styler.dpi)
    fig.patch.set_facecolor(styler.fig_bg)
    ax1.set_facecolor(styler.fig_bg)
    l1, = ax1.plot(
        df["threshold"], df["potential_gw"],
        color=color, lw=2.5, marker="o", ms=5, label="Potential (GW)",
    )
    ax1.fill_between(df["threshold"], 0, df["potential_gw"], color=color, alpha=0.10)
    ax1.axvline(base_thr, color="#374151", ls="--", alpha=0.8, label=f"Base ({base_thr:.2f})")
    ax1.set_xlabel("Suitability Threshold", fontsize=10)
    ax1.set_ylabel("Technical Potential (GW)", fontsize=10, color=color)
    ax2  = ax1.twinx()
    el   = df["elasticity"].dropna()
    l2,  = ax2.plot(
        df.loc[el.index, "threshold"], el,
        color="#7C3AED", lw=1.8, ls="--", marker="s", ms=4, label="Elasticity ε",
    )
    ax2.axhline(-1.0, color="#7C3AED", ls=":", alpha=0.5)
    ax2.set_ylabel("Elasticity ε", fontsize=9, color="#7C3AED")
    ax1.legend([l1, l2], [l1.get_label(), l2.get_label()], fontsize=9, loc="upper right")
    ax1.set_title(
        f"SA-3 · Threshold Sweep — {TECH_LABEL.get(tech, tech)}",
        fontsize=12, fontweight="bold",
    )
    ax1.grid(color=_GRID_C, lw=0.6, alpha=0.8)
    _watermark(fig)
    fig.tight_layout()
    styler.save(fig, out_path)


# ── Figure 4: SA-4 (LCOE Monte Carlo uncertainty) ─────────────────────────────

def plot_sa4_lcoe(styler: GeoWorldStyler, df: pd.DataFrame, tech: str, out_path: Path) -> None:
    """Generate SA-4 LCOE uncertainty plot."""
    stats = df.attrs.get("stats", {})
    color = TECH_COLOR.get(tech, "#F59E0B")
    fig, (ax_h, ax_s) = plt.subplots(
        1, 2, figsize=(13, 5.5), dpi=styler.dpi,
        gridspec_kw={"width_ratios": [2.5, 1]},
    )
    fig.patch.set_facecolor(styler.fig_bg)
    ax_h.set_facecolor(styler.fig_bg)
    lc  = df["lcoe_usd_mwh"].values
    ax_h.hist(lc, bins=80, color=color, alpha=0.75, edgecolor="white", lw=0.3)
    p05 = stats.get("lcoe_p05", np.percentile(lc,  5))
    p50 = stats.get("lcoe_p50", np.percentile(lc, 50))
    p95 = stats.get("lcoe_p95", np.percentile(lc, 95))
    ax_h.axvline(p05, color="#1D4ED8", ls="--", label=f"P5 = {p05:.1f} USD/MWh")
    ax_h.axvline(p50, color="#111827", ls="-",  label=f"P50 = {p50:.1f} USD/MWh")
    ax_h.axvline(p95, color="#DC2626", ls="--", label=f"P95 = {p95:.1f} USD/MWh")
    ax_h.spines[["top", "right"]].set_visible(False)
    ax_h.set_xlabel("LCOE (USD/MWh)", fontsize=10)
    ax_h.legend(fontsize=9)
    ax_s.axis("off")
    ax_s.add_patch(mpatches.FancyBboxPatch(
        (0.01, 0.01), 0.98, 0.98, boxstyle="round,pad=0.02",
        facecolor="white", edgecolor="#D1D5DB",
    ))
    y = 0.97
    for lbl, val in [
        ("Nominal CAPEX", f"${stats.get('capex_nominal', 0):.0f}/kW"),
        ("Nominal OPEX",  f"${stats.get('opex_nominal',  0):.0f}/kW/yr"),
        ("Nominal CF",    f"{stats.get('cf_nominal',     0):.3f}"),
        ("P50",           f"{p50:.1f} USD/MWh"),
        ("CI90",          f"{p95 - p05:.1f} USD/MWh"),
    ]:
        ax_s.text(0.04, y, lbl + ":", fontsize=8.5, color=_MUTE_C)
        ax_s.text(0.96, y, val, fontsize=8.5, ha="right", fontweight="bold")
        y -= 0.08
    fig.suptitle(
        f"SA-4 · LCOE Uncertainty — {TECH_LABEL.get(tech, tech)}",
        fontsize=12, fontweight="bold",
    )
    _watermark(fig)
    fig.tight_layout()
    styler.save(fig, out_path)


# ── Figure 5: SA-5 (Sobol global sensitivity) ─────────────────────────────────

def plot_sa5_sobol(styler: GeoWorldStyler, df: pd.DataFrame, out_path: Path) -> None:
    """Generate SA-5 Sobol indices barplot."""
    fig, ax = plt.subplots(figsize=(9, 5), dpi=styler.dpi)
    fig.patch.set_facecolor(styler.fig_bg)
    ax.set_facecolor(styler.fig_bg)
    x = np.arange(len(df))
    w = 0.36
    ax.bar(x - w / 2, df["S1"], w, color="#2563EB", label="S1 (1st order)", alpha=0.82)
    ax.bar(x + w / 2, df["ST"], w, color="#7C3AED", label="ST (total)",     alpha=0.82)
    ax.errorbar(x - w / 2, df["S1"], yerr=df["S1_conf"], fmt="none", color="#1D4ED8", capsize=4)
    ax.errorbar(x + w / 2, df["ST"], yerr=df["ST_conf"], fmt="none", color="#5B21B6", capsize=4)
    ax.axhline(0.10, color="#DC2626", ls="--", label="S1=0.10 (dominant)")
    for i, v in enumerate(df["ST"]):
        ax.text(i + w / 2, v + 0.01, f"{v:.3f}", ha="center", fontsize=8)
    ax.set_xticks(x)
    ax.set_xticklabels(df["parameter"], rotation=12, ha="right", fontsize=9)
    ax.set_title("SA-5 · Sobol Indices (GHG Abatement Function)", fontsize=12, fontweight="bold")
    ax.spines[["top", "right"]].set_visible(False)
    ax.legend(fontsize=9)
    _watermark(fig)
    fig.tight_layout()
    styler.save(fig, out_path)


# ── Figure 6: SA-6 (potential parameter sensitivity) ──────────────────────────

def plot_sa6_potential(styler: GeoWorldStyler, df: pd.DataFrame, tech: str, out_path: Path) -> None:
    """Generate SA-6 potential sensitivity plots."""
    p_colors = {
        "power_density_mw_km2": "#DC2626",
        "land_use_factor":       "#2563EB",
        "capacity_factor":       "#16A34A",
    }
    p_labels = {
        "power_density_mw_km2": "Power Density",
        "land_use_factor":       "Land Use",
        "capacity_factor":       "Capacity Factor",
    }
    fig, (ax_l, ax_b) = plt.subplots(1, 2, figsize=(14, 5.5), dpi=styler.dpi)
    fig.patch.set_facecolor(styler.fig_bg)
    ax_l.set_facecolor(styler.fig_bg)
    ax_b.set_facecolor(styler.fig_bg)
    for param in df["parameter"].unique():
        sub = df[df["parameter"] == param].sort_values("perturbation_pct")
        c   = p_colors.get(param, "#888")
        lbl = p_labels.get(param, param)
        ax_l.plot(sub["perturbation_pct"], sub["delta_gw_pct"],
                  color=c, lw=2.2, marker="o", label=f"{lbl} (GW)")
        if param == "capacity_factor":
            ax_l.plot(sub["perturbation_pct"], sub["delta_twh_pct"],
                      color=c, ls="--", marker="^", label=f"{lbl} (TWh)")
    ax_l.axhline(0, color="#374151")
    ax_l.axvline(0, color="#D1D5DB", ls="--")
    ax_l.set_title("OAT Curves — ΔGW (and ΔTWH for CF)", fontsize=10, fontweight="bold")
    ax_l.set_xlabel("Perturbation (%)", fontsize=9)
    ax_l.set_ylabel("ΔPotential (%)", fontsize=9)
    ax_l.legend(fontsize=8.5)
    ax_l.spines[["top", "right"]].set_visible(False)
    s30 = (
        df[df["perturbation_pct"] == 30.0]
        .assign(abs_dgw=lambda x: x["delta_gw_pct"].abs())
        .sort_values("abs_dgw", ascending=False)
    )
    bars = ax_b.barh(
        [p_labels.get(p, p) for p in s30["parameter"]],
        s30["abs_dgw"],
        color=[p_colors.get(p, "#888") for p in s30["parameter"]],
        alpha=0.82,
    )
    for bar, v in zip(bars, s30["abs_dgw"]):
        ax_b.text(
            bar.get_width() + 0.5, bar.get_y() + bar.get_height() / 2,
            f"{v:.1f}%", va="center", fontsize=9, fontweight="bold",
        )
    ax_b.set_title("Elasticity at +30% perturbation", fontsize=10, fontweight="bold")
    ax_b.spines[["top", "right"]].set_visible(False)
    fig.suptitle(
        f"SA-6 · Potential Sensitivity — {TECH_LABEL.get(tech, tech)}",
        fontsize=12, fontweight="bold",
    )
    _watermark(fig)
    fig.tight_layout()
    styler.save(fig, out_path)


# ── Figure 7: Comprehensive dashboard ─────────────────────────────────────────

def plot_dashboard(
    styler: GeoWorldStyler,
    rs: Dict[str, Any],
    tech: str,
    country_name: str,
    out_path: Path,
) -> None:
    """Generate comprehensive sensitivity dashboard."""
    td    = rs.get(tech, {})
    color = TECH_COLOR.get(tech, "#2563EB")
    fig   = plt.figure(figsize=(22, 12), dpi=styler.dpi)
    fig.patch.set_facecolor(styler.fig_bg)
    gs    = gridspec.GridSpec(
        2, 4, figure=fig,
        hspace=0.46, wspace=0.38,
        left=0.05, right=0.97, top=0.91, bottom=0.06,
    )
    axes = [fig.add_subplot(gs[r, c]) for r in range(2) for c in range(4)]

    for ax, lt in zip(axes, "abcdefgh"):
        ax.set_facecolor(styler.fig_bg)
        ax.text(0.02, 0.97, f"({lt})", transform=ax.transAxes,
                fontsize=9.5, fontweight="bold", va="top", zorder=10)

    # (a) SA-1 Tornado
    ax = axes[0]
    ax.set_title("SA-1 · OAT Sensitivity\n(1−ρ_min per criterion)",
                 fontsize=9, fontweight="bold")
    if "sa1" in td and "_df" in td["sa1"]:
        sm = (
            td["sa1"]["_df"].groupby("criterion")["spearman_rho"].min()
            .reset_index().sort_values("spearman_rho").head(8)
        )
        cl = [
            "#16a34a" if r >= 0.95 else "#d97706" if r >= 0.90 else "#dc2626"
            for r in sm["spearman_rho"]
        ]
        ax.barh(range(len(sm)), 1 - sm["spearman_rho"], color=cl, alpha=0.82)
        ax.set_yticks(range(len(sm)))
        ax.set_yticklabels(sm["criterion"], fontsize=7.5)
        ax.axvline(0.05, color="#dc2626", ls="--", alpha=0.6)
        ax.set_xlabel("1 − ρ_min", fontsize=8)
    else:
        ax.text(0.5, 0.5, "SA-1 not executed", ha="center", color=_MUTE_C, fontsize=9)
    ax.spines[["top", "right"]].set_visible(False)

    # (b) SA-2 CV
    ax = axes[1]
    ax.set_title("SA-2 · MC AHP\nCV Distribution", fontsize=9, fontweight="bold")
    if "sa2" in td and "_cv" in td["sa2"]:
        cv_c = td["sa2"]["_cv"]
        cv_c = cv_c[np.isfinite(cv_c) & (cv_c >= 0)]
        if cv_c.size > 0:
            ax.hist(cv_c, bins=45, color=color, alpha=0.75, edgecolor="white")
            med = float(np.median(cv_c))
            ax.axvline(med, color="#111827", ls="--", label=f"Med={med:.3f}")
            ax.legend(fontsize=7.5)
    else:
        ax.text(0.5, 0.5, "SA-2 not executed", ha="center", color=_MUTE_C, fontsize=9)
    ax.set_xlabel("CV per pixel", fontsize=8)
    ax.spines[["top", "right"]].set_visible(False)

    # (c) SA-3 Threshold
    ax = axes[2]
    ax.set_title("SA-3 · Threshold Sweep\nPotential vs Threshold",
                 fontsize=9, fontweight="bold")
    if "sa3" in td and "_df" in td["sa3"]:
        df3 = td["sa3"]["_df"]
        ax.plot(df3["threshold"], df3["potential_gw"], color=color, lw=2.0, marker="o")
        ax.fill_between(df3["threshold"], 0, df3["potential_gw"], color=color, alpha=0.10)
        ax.axvline(td["sa3"].get("base_threshold", 0.60), color="#374151", ls="--", alpha=0.8)
    else:
        ax.text(0.5, 0.5, "SA-3 not executed", ha="center", color=_MUTE_C, fontsize=9)
    ax.set_xlabel("Threshold", fontsize=8)
    ax.set_ylabel("GW", fontsize=8)
    ax.spines[["top", "right"]].set_visible(False)

    # (d) SA-4 LCOE
    ax = axes[3]
    ax.set_title("SA-4 · LCOE MC\n$/MWh Histogram", fontsize=9, fontweight="bold")
    if "sa4" in td and "_df" in td["sa4"]:
        lc = td["sa4"]["_df"]["lcoe_usd_mwh"].values
        st = td["sa4"]["_df"].attrs.get("stats", td["sa4"])
        ax.hist(lc, bins=50, color=color, alpha=0.75, edgecolor="white")
        for p, pc, l_lbl in [
            (st.get("lcoe_p05", 0), "#1D4ED8", "P5"),
            (st.get("lcoe_p50", 0), "#111827", "P50"),
            (st.get("lcoe_p95", 0), "#DC2626", "P95"),
        ]:
            ax.axvline(p, color=pc,
                       ls="--" if l_lbl != "P50" else "-",
                       label=f"{l_lbl}={p:.0f}")
        ax.legend(fontsize=7.5)
    else:
        ax.text(0.5, 0.5, "SA-4 not executed", ha="center", color=_MUTE_C, fontsize=9)
    ax.set_xlabel("LCOE ($/MWh)", fontsize=8)
    ax.spines[["top", "right"]].set_visible(False)

    # (e) SA-6 OAT
    ax = axes[4]
    ax.set_title("SA-6 · Potential Sensitivity\nΔGW (%) per parameter",
                 fontsize=9, fontweight="bold")
    p_c = {
        "power_density_mw_km2": "#DC2626",
        "land_use_factor":       "#2563EB",
        "capacity_factor":       "#16A34A",
    }
    p_s = {
        "power_density_mw_km2": "PD",
        "land_use_factor":       "LU",
        "capacity_factor":       "CF",
    }
    if "sa6" in td and "_df" in td["sa6"]:
        df6 = td["sa6"]["_df"]
        for param in df6["parameter"].unique():
            sub = df6[df6["parameter"] == param].sort_values("perturbation_pct")
            ax.plot(sub["perturbation_pct"], sub["delta_gw_pct"],
                    color=p_c.get(param, "#888"), label=p_s.get(param, param))
        ax.axhline(0, color="#374151")
        ax.legend(fontsize=7.5)
    else:
        ax.text(0.5, 0.5, "SA-6 not executed", ha="center", color=_MUTE_C, fontsize=9)
    ax.set_xlabel("Perturbation (%)", fontsize=8)
    ax.spines[["top", "right"]].set_visible(False)

    # (f) SA-1 Heatmap
    ax = axes[5]
    ax.set_title("SA-1 · Heatmap ρ\n(Top 6 criteria)", fontsize=9, fontweight="bold")
    if "sa1" in td and "_df" in td["sa1"]:
        df1  = td["sa1"]["_df"]
        top6 = df1.groupby("criterion")["spearman_rho"].min().sort_values().head(6).index
        piv  = (
            df1[df1["criterion"].isin(top6)]
            .pivot_table(
                index="criterion", columns="perturbation_pct",
                values="spearman_rho", aggfunc="mean",
            )
        )
        piv = piv.loc[piv.min(axis=1).sort_values().index]
        if not piv.empty:
            ax.imshow(piv.values, cmap="RdYlGn", vmin=0.85, vmax=1.0, aspect="auto")
            ax.set_xticks(range(len(piv.columns)))
            ax.set_xticklabels([f"{int(v)}%" for v in piv.columns], fontsize=6.5)
            ax.set_yticks(range(len(piv.index)))
            ax.set_yticklabels(piv.index, fontsize=7)
    else:
        ax.text(0.5, 0.5, "SA-1 not executed", ha="center", color=_MUTE_C, fontsize=9)

    # (g) SA-1 + SA-2 KPIs
    ax = axes[6]
    ax.axis("off")
    ax.set_title("SA-1 + SA-2 Summary", fontsize=9, fontweight="bold")
    kpis_g: List[Tuple[str, str]] = []
    if "sa1" in td:
        kpis_g += [
            ("SA-1 Robust criteria",    str(td["sa1"].get("n_robust",       "—"))),
            ("SA-1 Sensitive criteria", str(td["sa1"].get("n_sensitive",    "—"))),
            ("SA-1 Global min ρ",       f"{td['sa1'].get('rho_min_global', 0.0):.4f}"),
        ]
    if "sa2" in td:
        kpis_g += [
            ("SA-2 Stable pixels", f"{td['sa2'].get('stable_fraction_90ci', 0) * 100:.1f}%"),
            ("SA-2 Mean CV",       f"{td['sa2'].get('mean_cv', 0):.4f}"),
        ]
    _draw_kpis(ax, kpis_g)

    # (h) SA-3 + SA-4 + SA-6 KPIs
    ax = axes[7]
    ax.axis("off")
    ax.set_title("SA-3 + SA-4 + SA-6 Summary", fontsize=9, fontweight="bold")
    kpis_h: List[Tuple[str, str]] = []
    if "sa3" in td:
        df3 = td["sa3"].get("_df")
        bt  = td["sa3"].get("base_threshold", 0.6)
        if df3 is not None and not df3[abs(df3["threshold"] - bt) < 0.001].empty:
            kpis_h.append((
                "SA-3 GW @ balanced",
                f"{df3[abs(df3['threshold'] - bt) < 0.001]['potential_gw'].values[0]:.2f}",
            ))
    if "sa4" in td:
        kpis_h += [
            ("SA-4 P50 $/MWh", f"{td['sa4'].get('lcoe_p50', '—')}"),
            ("SA-4 CI90",      f"{td['sa4'].get('ci90_width', '—')} $/MWh"),
        ]
    if "sa6" in td:
        kpis_h.append(("SA-6 Base GW", str(td["sa6"].get("base_gw", "—"))))
    _draw_kpis(ax, kpis_h)

    fig.suptitle(
        f"Sensitivity Dashboard — {TECH_LABEL.get(tech, tech)} · {country_name}",
        fontsize=14, fontweight="bold", y=0.97,
    )
    _watermark(fig)
    styler.save(fig, out_path)
