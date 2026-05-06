"""
plots_abatement.py — Visualizations for Phase 7 (GHG Abatement).
Separated from GHGAbatementCalculator to keep the main module lean.
Each function receives a GeoWorldStyler and the necessary data — no global state.

SCOPE: all figures exclusively cover the ELECTRICITY GENERATION sector.
Total national CO₂ (used in waterfall and gap analysis) includes all sectors,
with the electricity sector being a fraction of that total — clarified in each chart.
"""
from __future__ import annotations

import logging
import math
import warnings
from datetime import date
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

import geopandas as gpd
import matplotlib.lines as mlines
import matplotlib.patches as mpatches
import matplotlib.patheffects as patheffects
import matplotlib.pyplot as plt
import matplotlib.ticker as mticker
import numpy as np
import pandas as pd

from src.utils.map_styling import GeoWorldStyler

logger = logging.getLogger("geoworld.utils.plots_abatement")

# ── Visual constants ──────────────────────────────────────────────────────────
COLORS = {
    "coal": "#7f1d1d",
    "gas": "#c2410c",
    "oil": "#78350f",
    "solar": "#d97706",
    "wind": "#1d4ed8",
    "biomass": "#15803d",
}
TECH_LABELS = {
    "solar": "Solar PV",
    "wind": "Wind Onshore",
    "biomass": "Biomass"
}

# Local fallbacks (mirror those in main module — avoid circular import)
_CF_FALLBACK = {"solar": 0.20, "wind": 0.30, "biomass": 0.75}
_EF_FALLBACK = {"solar": 48.0, "wind": 11.0, "biomass": 230.0}


# ── Private helper ────────────────────────────────────────────────────────────

def _build_thermal_geodf(
    plants_df: pd.DataFrame,
    thermal_params: Dict[str, Dict[str, float]],
) -> Optional[gpd.GeoDataFrame]:
    """
    Construct GeoDataFrame of thermal plants with calculated co2_kt_yr.

    Args:
        plants_df: DataFrame of power plants.
        thermal_params: Dictionary of thermal technology parameters.

    Returns:
        GeoDataFrame with thermal plants or None if unavailable.
    """
    if plants_df is None or plants_df.empty:
        return None

    df = plants_df.copy()
    df.columns = [c.lower().strip() for c in df.columns]

    lat = next(
        (c for c in ["latitude", "lat"] if c in df.columns),
        None
    )
    lon = next(
        (c for c in ["longitude", "lon", "long"] if c in df.columns),
        None
    )
    fuel = next(
        (c for c in ["primary_fuel", "fuel1"] if c in df.columns),
        None
    )
    cap = next(
        (c for c in ["capacity_mw", "capacity"] if c in df.columns),
        None
    )

    if not all([lat, lon, fuel, cap]):
        return None

    df[fuel] = df[fuel].astype(str).str.lower().str.strip()
    df = df[df[fuel].isin(thermal_params.keys())].copy()
    df[cap] = pd.to_numeric(df[cap], errors="coerce")
    df = df.dropna(subset=[cap, lat, lon])

    if df.empty:
        return None

    df["cf"] = df[fuel].map(
        {k: v["cf"] for k, v in thermal_params.items()}
    ).fillna(0.45)
    df["ef"] = df[fuel].map(
        {k: v["ef"] for k, v in thermal_params.items()}
    ).fillna(500.0)
    df["gen_gwh"] = df[cap] * df["cf"] * 8760 / 1000
    df["co2_kt_yr"] = df["gen_gwh"] * df["ef"] / 1000
    df["fuel"] = df[fuel]
    df["capacity_mw"] = df[cap]

    return gpd.GeoDataFrame(
        df,
        geometry=gpd.points_from_xy(df[lon], df[lat]),
        crs="EPSG:4326"
    )


def plot_geography(
    styler: GeoWorldStyler,
    result: Dict[str, Any],
    plants_df: pd.DataFrame,
    zonal_dfs: Dict[str, pd.DataFrame],
    mainland_gdf: gpd.GeoDataFrame,
    context_gdf: Optional[gpd.GeoDataFrame],
    admin_gdf: Optional[gpd.GeoDataFrame],
    country_name: str,
    out_path: Path,
    thermal_params: Dict[str, Dict[str, float]],
    renewable_cf: Dict[str, float],
) -> None:
    """
    Generate dual-panel geography map: thermal hotspots vs renewable hubs.

    Args:
        styler: GeoWorldStyler instance for consistent styling.
        result: Phase 7 abatement results dictionary.
        plants_df: DataFrame of power plants.
        zonal_dfs: Dictionary of zonal aggregation DataFrames per technology.
        mainland_gdf: GeoDataFrame of mainland geometry.
        context_gdf: Optional GeoDataFrame for context (e.g., neighbors).
        admin_gdf: Optional GeoDataFrame of administrative boundaries.
        country_name: Country name for title.
        out_path: Output file path.
        thermal_params: Dictionary of thermal technology parameters.
        renewable_cf: Dictionary of renewable capacity factors.
    """
    minx, miny, maxx, maxy = mainland_gdf.total_bounds
    lon_span = maxx - minx
    lat_span = maxy - miny

    PAD_FRAC = 0.04
    px = lon_span * PAD_FRAC
    py = lat_span * PAD_FRAC
    x0 = minx - px
    x1 = maxx + px
    y0 = miny - py
    y1 = maxy + py
    extent = [x0, x1, y0, y1]

    lat_mid_rad = math.radians((miny + maxy) / 2.0)
    fw = 20.0
    map_h = (fw * 0.43) * (lat_span / lon_span) / math.cos(lat_mid_rad)

    fig = plt.figure(
        figsize=(fw, max(10.0, map_h + 1.8)),
        dpi=styler.dpi
    )
    fig.patch.set_facecolor(styler.fig_bg)

    title_main = (
        f"Abatement Geography: Thermal Hotspots vs Renewable Hubs — "
        f"{country_name}"
    )
    styler.add_standard_title(fig, title_main, y_main=0.99)

    ax1 = fig.add_axes([0.03, 0.12, 0.44, 0.82])
    ax2 = fig.add_axes([0.52, 0.12, 0.44, 0.82])

    for ax in [ax1, ax2]:
        ax.set_facecolor("#DDEEFF")
        ax.set_xticks([])
        ax.set_yticks([])
        styler.draw_basemap(
            ax,
            "EPSG:4326",
            mainland_gdf,
            context_gdf,
            admin_gdf,
            extent=extent
        )
        ax.set_xlim(x0, x1)
        ax.set_ylim(y0, y1)

    ax1.set_title(
        "(A) Thermal Emission Hotspots",
        fontsize=12,
        fontweight="bold",
        pad=10
    )
    ax2.set_title(
        "(B) Top Renewable Hubs (by District)",
        fontsize=12,
        fontweight="bold",
        pad=10
    )

    def _get_centroids(gdf):
        """Calculate centroids with UTM projection."""
        try:
            utm_crs = gdf.estimate_utm_crs()
            return gdf.to_crs(utm_crs).geometry.centroid.to_crs("EPSG:4326")
        except Exception:
            return gdf.geometry.centroid

    def _draw_district_labels(
        ax,
        gdf,
        centroids,
        name_col,
        x0,
        x1,
        y0,
        y1,
        fontsize=11,
        zorder=10
    ):
        """Draw district labels with path effects."""
        for idx, row in gdf.iterrows():
            cx = centroids[idx].x
            cy = centroids[idx].y

            if not (x0 <= cx <= x1 and y0 <= cy <= y1):
                continue

            name = str(row[name_col]).title()
            txt = ax.text(
                cx,
                cy,
                name,
                fontsize=fontsize,
                ha="center",
                va="center",
                color="#1A1A1A",
                fontweight="bold",
                zorder=zorder,
                alpha=0.90
            )
            txt.set_path_effects([
                patheffects.withStroke(
                    linewidth=2.5,
                    foreground="white",
                    alpha=0.85
                )
            ])

    # ── Panel A — Thermal plants ──────────────────────────────────────────────
    tgdf = _build_thermal_geodf(plants_df, thermal_params)

    if tgdf is not None and len(tgdf) > 0:
        tgdf = tgdf[
            (tgdf.geometry.x >= minx) & (tgdf.geometry.x <= maxx) &
            (tgdf.geometry.y >= miny) & (tgdf.geometry.y <= maxy)
        ]

        if len(tgdf) > 0:
            max_co2 = float(tgdf["co2_kt_yr"].max())

            for _, row in tgdf.sort_values("co2_kt_yr").iterrows():
                color = COLORS.get(str(row["fuel"]), "#555")
                ratio = (
                    float(row["co2_kt_yr"]) / max_co2
                    if max_co2 > 0
                    else 0.1
                )

                ax1.add_patch(
                    plt.Circle(
                        (row.geometry.x, row.geometry.y),
                        lat_span * 0.055 * math.sqrt(ratio),
                        color=color,
                        alpha=0.11,
                        zorder=5,
                        linewidth=0,
                    )
                )
                ax1.scatter(
                    row.geometry.x,
                    row.geometry.y,
                    s=max(60, 650 * ratio ** 0.55),
                    c=color,
                    edgecolors="white",
                    linewidths=1.3,
                    zorder=7,
                    alpha=0.92,
                )

                if ratio > 0.30:
                    txt = ax1.text(
                        row.geometry.x,
                        row.geometry.y + lat_span * 0.028,
                        f"{row.get('capacity_mw', 0):.0f} MW",
                        fontsize=11,
                        ha="center",
                        va="bottom",
                        color=color,
                        fontweight="bold",
                        zorder=9,
                    )
                    txt.set_path_effects([
                        patheffects.withStroke(
                            linewidth=2.0,
                            foreground="white"
                        )
                    ])

    # District labels — Panel A
    if admin_gdf is not None and not admin_gdf.empty:
        name_col_a = next(
            (c for c in [
                "NAME_1",
                "NM_DISTRI",
                "VARNAME_1",
                "NAME",
                "_admin_name"
            ] if c in admin_gdf.columns),
            None
        )

        if name_col_a:
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", UserWarning)
                centroids_a = _get_centroids(admin_gdf)

            _draw_district_labels(
                ax1,
                admin_gdf,
                centroids_a,
                name_col_a,
                x0,
                x1,
                y0,
                y1,
                fontsize=11,
                zorder=10,
            )

    # ── Panel B — Renewables (with robust anti-overlap) ───────────────────────
    if admin_gdf is not None and not admin_gdf.empty and zonal_dfs:
        name_col = next(
            (c for c in [
                "NAME_1",
                "NM_DISTRI",
                "VARNAME_1",
                "NAME",
                "_admin_name"
            ] if c in admin_gdf.columns),
            None
        )

        if name_col:
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", UserWarning)
                centroids_b = _get_centroids(admin_gdf)

            in_mainland = (
                (centroids_b.x >= minx) & (centroids_b.x <= maxx) &
                (centroids_b.y >= miny) & (centroids_b.y <= maxy)
            )
            admin_mainland = admin_gdf[in_mainland].copy()
            centroids_m = centroids_b[in_mainland]

            coord_map: Dict[str, Tuple[float, float]] = {}

            for idx, row in admin_mainland.iterrows():
                nm = str(row[name_col]).upper()
                coord_map[nm] = (centroids_m[idx].x, centroids_m[idx].y)

            TECH_LAYOUT = {
                "solar": {
                    "ox": -lon_span * 0.028,
                    "oy": 0,
                    "label_va": "top",
                    "label_dy": -lat_span * 0.035,
                    "color": COLORS["solar"]
                },
                "wind": {
                    "ox": 0,
                    "oy": 0,
                    "label_va": "bottom",
                    "label_dy": +lat_span * 0.038,
                    "color": COLORS["wind"]
                },
                "biomass": {
                    "ox": lon_span * 0.028,
                    "oy": 0,
                    "label_va": "top",
                    "label_dy": -lat_span * 0.035,
                    "color": COLORS["biomass"]
                },
            }

            district_techs: Dict[str, Dict[str, Any]] = {}

            for tech in ["solar", "wind", "biomass"]:
                zdf = zonal_dfs.get(tech, pd.DataFrame())

                if zdf.empty or "capacity_mw_sum" not in zdf.columns:
                    continue

                adm_col = next(
                    (c for c in ["admin_name", "NAME_1"] if c in zdf.columns),
                    None
                )

                if not adm_col:
                    continue

                top = zdf.nlargest(8, "capacity_mw_sum")
                max_cap = (
                    float(top["capacity_mw_sum"].max())
                    if not top.empty
                    else 1.0
                )

                for _, row in top.iterrows():
                    nm = str(row[adm_col]).upper()

                    if nm not in coord_map:
                        continue

                    if nm not in district_techs:
                        district_techs[nm] = {}

                    district_techs[nm][tech] = {
                        "cap": float(row["capacity_mw_sum"]),
                        "ratio": (
                            float(row["capacity_mw_sum"]) / max_cap
                            if max_cap > 0
                            else 0.1
                        ),
                        "lon": coord_map[nm][0],
                        "lat": coord_map[nm][1],
                    }

            # Plot circles first (all)
            for nm, techs in district_techs.items():
                for tech, data in techs.items():
                    cfg = TECH_LAYOUT[tech]
                    ax2.scatter(
                        data["lon"] + cfg["ox"],
                        data["lat"] + cfg["oy"],
                        s=max(80, 800 * data["ratio"] ** 0.6),
                        c=cfg["color"],
                        alpha=0.78,
                        edgecolors="white",
                        linewidths=1.4,
                        zorder=8,
                    )

            # Plot GW labels with smart positioning
            for nm, techs in district_techs.items():
                n_techs = len(techs)

                for tech, data in techs.items():
                    if data["ratio"] < 0.10:
                        continue

                    cfg = TECH_LAYOUT[tech]

                    dx_label = 0
                    dy_label = cfg["label_dy"]

                    if n_techs >= 2:
                        if tech == "solar":
                            dx_label = -lon_span * 0.008
                        elif tech == "biomass":
                            dx_label = lon_span * 0.008

                    txt = ax2.text(
                        data["lon"] + cfg["ox"] + dx_label,
                        data["lat"] + cfg["oy"] + dy_label,
                        f"{data['cap'] / 1000:.1f} GW",
                        fontsize=10,
                        ha="center",
                        va=cfg["label_va"],
                        color=cfg["color"],
                        alpha=0.95,
                        fontweight="bold",
                        zorder=9,
                    )
                    txt.set_path_effects([
                        patheffects.withStroke(
                            linewidth=2.2,
                            foreground="white"
                        )
                    ])

            # District names (centered, on top of everything)
            admin_plotted = admin_mainland[
                admin_mainland[name_col].str.upper().isin(
                    district_techs.keys()
                )
            ]

            if not admin_plotted.empty:
                centroids_plotted = centroids_m[admin_plotted.index]
                _draw_district_labels(
                    ax2,
                    admin_plotted,
                    centroids_plotted,
                    name_col,
                    x0,
                    x1,
                    y0,
                    y1,
                    fontsize=11,
                    zorder=11,
                )

    # ── Legends ───────────────────────────────────────────────────────────────
    if tgdf is not None and len(tgdf) > 0:
        ax1.legend(
            handles=[
                mlines.Line2D(
                    [0],
                    [0],
                    marker="o",
                    color="w",
                    markerfacecolor=COLORS.get(f, "#555"),
                    markersize=11,
                    label=f.capitalize()
                )
                for f in sorted(tgdf["fuel"].unique())
            ],
            loc="lower right",
            fontsize=9,
            title="Thermal Fuels",
            title_fontsize=9,
            framealpha=0.94,
        )

    techs_used = [
        t for t in ["solar", "wind", "biomass"]
        if not zonal_dfs.get(t, pd.DataFrame()).empty
    ]

    if techs_used:
        ax2.legend(
            handles=[
                mlines.Line2D(
                    [0],
                    [0],
                    marker="o",
                    color="w",
                    markerfacecolor=COLORS[t],
                    markersize=11,
                    label=TECH_LABELS[t]
                )
                for t in techs_used
            ],
            loc="lower right",
            fontsize=9,
            title="Renewable Tech",
            title_fontsize=9,
            framealpha=0.94,
        )

    # ── Footer ────────────────────────────────────────────────────────────────
    co2 = result["co2_avoided_mt"]
    fig.text(
        0.5,
        0.048,
        f"CO₂ Avoided: {co2:.2f} MtCO₂e/yr  ·  "
        f"Carbon Value: ${result['carbon_value_b']:.2f}B  ·  "
        f"Fuel Savings: ${result['fuel_savings_b']:.2f}B",
        ha="center",
        fontsize=13,
        bbox=dict(
            boxstyle="round,pad=0.5",
            facecolor="#EEF2FF",
            edgecolor="#b0b8e8",
            alpha=0.94
        ),
    )

    mac = result["mac_global"]
    mac_lbl = (
        f"Self-financing (MAC ${mac:.1f}/tCO₂e)"
        if mac <= 0
        else f"MAC ${mac:.1f}/tCO₂e"
    )

    fig.text(
        0.5,
        0.016,
        f"{mac_lbl}  ·  Carbon price: ${result['carbon_price']:.0f}/tCO₂e   |   "
        "Bubble size ∝ CO₂ (thermal) | GW (renewable)",
        ha="center",
        fontsize=11,
        color="#555555",
        style="italic",
    )

    styler.add_standard_footer(fig)
    styler.save(fig, out_path)
    logger.info("Geography map saved: %s", out_path.name)


# ── Figure 2: MACC ────────────────────────────────────────────────────────────

def plot_macc_curve(
    styler: GeoWorldStyler,
    result: Dict[str, Any],
    country_name: str,
    out_path: Path,
) -> None:
    """
    Generate MACC (Marginal Abatement Cost Curve) with thermal fleet panel.

    Args:
        styler: GeoWorldStyler instance for consistent styling.
        result: Phase 7 abatement results dictionary.
        country_name: Country name for title.
        out_path: Output file path.
    """
    by_tech = result["by_tech"]

    if not by_tech:
        return

    total_th_gwh = result["total_thermal_gwh"]
    total_th_co2 = result["total_thermal_co2"]
    fleet_df = result["fleet_df"]

    bars = sorted(
        [
            {
                "label": TECH_LABELS.get(
                    t["tech"],
                    t["tech"].capitalize()
                ),
                "tech": t["tech"],
                "mac": t["mac_usd_tco2e"],
                "width": (
                    total_th_co2
                    * min(
                        t["generation_gwh"] / max(total_th_gwh, 1),
                        1.0
                    )
                    * result.get("penetration", 1.0)
                ),
                "color": (
                    "#16a34a" if t["mac_usd_tco2e"] <= 0
                    else "#ea580c" if t["mac_usd_tco2e"] <= 100
                    else "#b91c1c"
                ),
                "lcoe": t["lcoe_usd_mwh"],
                "gwh": t["generation_gwh"],
            }
            for t in by_tech
        ],
        key=lambda b: b["mac"]
    )

    fig, (ax_macc, ax_fleet) = plt.subplots(
        1,
        2,
        figsize=(16, 6),
        dpi=styler.dpi,
        gridspec_kw={
            "width_ratios": [2.8, 1.2],
            "wspace": 0.15,
            "bottom": 0.18,
            "right": 0.96
        }
    )
    fig.patch.set_facecolor(styler.fig_bg)

    title_main = f"Marginal Abatement Cost Analysis — {country_name}"
    styler.add_standard_title(fig, title_main, y_main=1.0)

    ax_macc.set_facecolor("#FAFAFA")
    ax_macc.set_title(
        "(A) Marginal Abatement Cost Curve (MACC)",
        fontsize=10,
        fontweight="bold"
    )

    x = 0.0
    max_abs_mac = max([abs(b["mac"]) for b in bars]) if bars else 100

    for bar in bars:
        y0 = min(0.0, bar["mac"])
        h = abs(bar["mac"]) if bar["mac"] != 0 else 0.3

        ax_macc.add_patch(
            mpatches.FancyBboxPatch(
                (x, y0),
                bar["width"],
                h,
                boxstyle="square,pad=0",
                facecolor=bar["color"],
                alpha=0.82,
                edgecolor="white",
                linewidth=1.5,
                zorder=4
            )
        )

        y_offset = max(8, max_abs_mac * 0.08)

        if bar["mac"] >= 0:
            y_text = bar["mac"] + y_offset
        else:
            y_text = y0 - (y_offset * 0.8)

        if abs(bar["mac"]) > 120:
            y_text = bar["mac"] * 0.8

        bbox_dict = None
        if abs(bar["mac"]) > 80:
            bbox_dict = dict(
                boxstyle="round,pad=0.25",
                facecolor="white",
                alpha=0.8,
                edgecolor="none"
            )

        ax_macc.text(
            x + bar["width"] / 2,
            y_text,
            f"{bar['label']}\n${bar['lcoe']:.0f}/MWh",
            ha="center",
            va="bottom" if bar["mac"] >= 0 else "top",
            fontsize=8.5,
            fontweight="bold",
            color=bar["color"],
            zorder=5,
            bbox=bbox_dict
        )

        if bar["width"] > x * 0.06 and abs(bar["mac"]) > 5:
            ax_macc.text(
                x + bar["width"] / 2,
                bar["mac"] / 2,
                f"${bar['mac']:.1f}",
                ha="center",
                va="center",
                fontsize=9,
                color="white",
                fontweight="bold",
                zorder=6
            )

        x += bar["width"]

    ax_macc.axhline(
        0,
        color="#111827",
        linewidth=2.0,
        zorder=3,
        alpha=0.85
    )
    ax_macc.axhspan(-300, 0, alpha=0.04, color="#16a34a")
    ax_macc.axhspan(0, 400, alpha=0.03, color="#b91c1c")

    cp = result.get("carbon_price", 0)
    ax_macc.axhline(
        cp,
        color="#7c3aed",
        linewidth=2.0,
        linestyle="--",
        zorder=3,
        label=f"Carbon price: ${cp:.0f}/tCO₂e"
    )

    y_cp_label = cp + (max_abs_mac * 0.08)
    ax_macc.text(
        x * 0.02,
        y_cp_label,
        f"${cp:.0f}/tCO₂e",
        ha="left",
        va="bottom",
        fontsize=9,
        color="#7c3aed",
        fontweight="bold",
        bbox=dict(
            boxstyle="round,pad=0.3",
            facecolor="white",
            edgecolor="#7c3aed",
            alpha=0.9
        )
    )

    ax_macc.set_xlabel(
        "Cumulative CO₂ Abatement Potential (MtCO₂e/yr)",
        fontsize=10
    )
    ax_macc.set_ylabel(
        "MAC — Marginal Abatement Cost (USD/tCO₂e)",
        fontsize=10
    )
    ax_macc.set_xlim(0, x * 1.08)

    y_all = [b["mac"] for b in bars] + [cp, 0]
    ym = max(abs(min(y_all)), abs(max(y_all))) * 0.25
    ax_macc.set_ylim(
        min(y_all) - ym - 10,
        max(y_all) + ym + 25
    )

    ax_macc.spines[["top", "right"]].set_visible(False)
    ax_macc.grid(axis="y", linestyle="--", alpha=0.35, color="#d1d5db")
    ax_macc.legend(fontsize=9, loc="upper left", framealpha=0.9)
    ax_macc.tick_params(labelsize=9)

    mac_g = result.get("mac_global", 0)

    if mac_g <= 0:
        interp = (
            "✓ SELF-FINANCING\n"
            f"MAC ${mac_g:.1f}/tCO₂e\n"
            "Renewables cheaper than thermal SRMC"
        )
    else:
        if cp >= mac_g:
            interp = (
                f"MAC ${mac_g:.1f}/tCO₂e\n"
                "Needs carbon support\n"
                "✓ EU ETS covers cost"
            )
        else:
            interp = (
                f"MAC ${mac_g:.1f}/tCO₂e\n"
                "Needs carbon support\n"
                "✗ EU ETS below breakeven"
            )

    ax_macc.text(
        0.98,
        0.97,
        interp,
        transform=ax_macc.transAxes,
        fontsize=8.5,
        va="top",
        ha="right",
        color="#15803d" if (mac_g <= 0 or cp >= mac_g) else "#b91c1c",
        bbox=dict(
            boxstyle="round,pad=0.4",
            facecolor="white",
            alpha=0.88,
            edgecolor="#d1d5db"
        )
    )

    # Panel B — thermal fleet
    ax_fleet.set_facecolor("#FAFAFA")
    ax_fleet.set_title(
        "(B) Thermal Fleet Generation\n"
        "Before vs After Substitution\n"
        "(GWh/yr · CO₂ per fuel)",
        fontsize=9,
        fontweight="bold"
    )

    if not fleet_df.empty and "subst_gwh" in fleet_df.columns:
        fuels = fleet_df["fuel"].tolist()
        gen_before = fleet_df["gen_gwh"].tolist()
        gen_remain = (
            (fleet_df["gen_gwh"] - fleet_df["subst_gwh"].fillna(0))
            .clip(lower=0)
            .tolist()
        )
        y = np.arange(len(fuels))
        h = 0.35

        bars_before = ax_fleet.barh(
            y + h / 2,
            gen_before,
            h,
            color=[COLORS.get(f, "#555") for f in fuels],
            alpha=0.85,
            edgecolor="white",
            label="Before"
        )
        bars_after = ax_fleet.barh(
            y - h / 2,
            gen_remain,
            h,
            color=[COLORS.get(f, "#555") for f in fuels],
            alpha=0.40,
            edgecolor="white",
            hatch="///",
            label="After (residual)"
        )

        ax_fleet.set_yticks(y)
        ax_fleet.set_yticklabels(
            [f.capitalize() for f in fuels],
            fontsize=9
        )
        ax_fleet.set_xlabel("Generation (GWh/yr)", fontsize=9)
        ax_fleet.spines[["top", "right"]].set_visible(False)
        ax_fleet.legend(fontsize=8, loc="lower right")

        ax_fleet.xaxis.set_major_formatter(
            mticker.StrMethodFormatter('{x:,.0f}')
        )
        ax_fleet.tick_params(axis='x', labelsize=8, rotation=30)

        max_gb = max(gen_before) if gen_before else 0

        for i, (fuel, gb, gr) in enumerate(zip(fuels, gen_before, gen_remain)):
            co2_b = fleet_df.loc[
                fleet_df["fuel"] == fuel,
                "co2_mt"
            ].values[0]

            co2_s = (
                fleet_df.loc[
                    fleet_df["fuel"] == fuel,
                    "co2_avoided"
                ].values[0]
                if "co2_avoided" in fleet_df.columns
                else 0
            )

            ax_fleet.text(
                gb + max_gb * 0.02,
                i + h / 2,
                f"{co2_b:.2f} Mt",
                va="center",
                ha="left",
                fontsize=8,
                color=COLORS.get(fuel, "#555"),
                fontweight="bold"
            )

            if co2_s > 0:
                x_pos = gb + max_gb * 0.02
                ax_fleet.text(
                    x_pos,
                    i - h / 2,
                    f"−{co2_s:.2f} Mt avoided",
                    va="center",
                    ha="left",
                    fontsize=8,
                    color="#15803d",
                    fontweight="bold",
                    bbox=dict(
                        boxstyle="round,pad=0.2",
                        facecolor="#f0fdf4",
                        edgecolor="#15803d",
                        alpha=0.8
                    )
                )

                if gb > gr:
                    ax_fleet.plot(
                        [gr, gb],
                        [i - h / 2, i - h / 2],
                        color="#15803d",
                        linewidth=2,
                        alpha=0.5,
                        zorder=1
                    )

        ax_fleet.set_xlim(0, max_gb * 1.60)

    fig.text(
        0.98,
        0.01,
        f"GeoWorld Framework | {date.today()}",
        ha="right",
        fontsize=7,
        color="#777"
    )

    styler.save(fig, out_path)
    logger.info("MACC curve saved: %s", out_path.name)


# ── Figure 3: Substitution / Sensitivity ──────────────────────────────────────

def plot_substitution(
    styler: GeoWorldStyler,
    result: Dict[str, Any],
    renew_gwh: Dict[str, float],
    country_name: str,
    out_path: Path,
    renewable_cf: Dict[str, float],
) -> None:
    """
    Generate CO₂ substitution analysis with economic value sensitivity.

    Args:
        styler: GeoWorldStyler instance for consistent styling.
        result: Phase 7 abatement results dictionary.
        renew_gwh: Dictionary of renewable generation by technology (GWh/yr).
        country_name: Country name for title.
        out_path: Output file path.
        renewable_cf: Dictionary of renewable capacity factors.
    """
    by_tech = sorted(result["by_tech"], key=lambda t: t["mac_usd_tco2e"])

    if not by_tech:
        return

    total_th_gwh = result["total_thermal_gwh"]
    total_th_co2 = result["total_thermal_co2"]
    penetration = result["penetration"]

    fig, axes = plt.subplots(
        1,
        2,
        figsize=(14, 6),
        dpi=styler.dpi,
        gridspec_kw={"wspace": 0.35}
    )
    fig.patch.set_facecolor(styler.fig_bg)

    title_main = f"CO₂ Substitution Analysis — {country_name}"
    styler.add_standard_title(fig, title_main, y_main=0.99)

    ax1 = axes[0]
    ax1.set_facecolor("#FAFAFA")
    ax1.set_title(
        "(A) Cumulative CO₂ Abatement by Technology\n"
        "(merit order: lowest MAC first)",
        fontsize=10,
        fontweight="bold"
    )

    cumul_gw = 0.0
    cumul_co2 = 0.0
    offsets = {"solar": (-55, 10), "wind": (12, -26), "biomass": (12, 10)}

    for t in by_tech:
        tech = t["tech"]
        gwh = t["generation_gwh"]
        cf = renewable_cf.get(tech, _CF_FALLBACK.get(tech, 0.25))
        gw = gwh / (cf * 8760)
        co2_this = (
            total_th_co2
            * min(gwh / max(total_th_gwh, 1), 1.0)
            * penetration
        )
        gw_seg = np.linspace(0, gw, 80)
        co2_seg = np.minimum(
            cumul_co2 + gw_seg / max(gw, 1e-9) * co2_this,
            total_th_co2 * penetration
        )
        color = COLORS[tech]
        x_plot = cumul_gw + gw_seg

        ax1.fill_between(
            x_plot,
            cumul_co2,
            co2_seg,
            color=color,
            alpha=0.22
        )
        ax1.plot(
            x_plot,
            co2_seg,
            color=color,
            linewidth=2.5,
            label=f"{TECH_LABELS[tech]}  (MAC ${t['mac_usd_tco2e']:.1f}/tCO₂)",
            solid_capstyle="round"
        )

        dx, dy = offsets.get(tech, (12, 8))
        y_pos = float(co2_seg[-1])

        if y_pos > total_th_co2 * penetration * 0.9:
            dy = -20

        ax1.annotate(
            f"{gw:.0f} GW · {co2_this:.3f} MtCO₂",
            xy=(float(x_plot[-1]), y_pos),
            xytext=(dx, dy),
            textcoords="offset points",
            fontsize=8,
            color=color,
            fontweight="bold",
            arrowprops=dict(
                arrowstyle="-",
                color=color,
                lw=1.0,
                alpha=0.65
            ),
            bbox=dict(
                boxstyle="round,pad=0.3",
                facecolor="white",
                alpha=0.6,
                edgecolor="none"
            )
        )

        cumul_gw += gw
        cumul_co2 = float(co2_seg[-1])

    ax1.axhline(
        total_th_co2 * penetration,
        color="#B71C1C",
        linewidth=1.8,
        linestyle="--",
        alpha=0.85,
        label=f"Max abatable ({penetration:.0%}: {total_th_co2 * penetration:.2f} MtCO₂)"
    )
    ax1.set_xlabel("Cumulative Installed Capacity (GW)", fontsize=10)
    ax1.set_ylabel("Cumulative CO₂ Avoided (MtCO₂e/yr)", fontsize=10)
    ax1.legend(fontsize=8, loc="lower right", framealpha=0.9)
    ax1.grid(linestyle="--", alpha=0.35, color="#d1d5db")
    ax1.spines[["top", "right"]].set_visible(False)
    ax1.tick_params(labelsize=9)

    ax2 = axes[1]
    ax2.set_facecolor("#FAFAFA")
    ax2.set_title(
        "(B) Economic Value vs Carbon Price Sensitivity",
        fontsize=10,
        fontweight="bold"
    )

    cp_range = np.linspace(0, 250, 200)
    co2_avd = result["co2_avoided_mt"]
    srmc = result["srmc_avg"]
    lcoe_r = result["lcoe_avg_renew"]
    subst = result["subst_gwh"]
    carbon_v = co2_avd * 1e6 * cp_range / 1e9
    fuel_save = np.maximum(0, (srmc - lcoe_r) * subst * 1000 / 1e9)

    ax2.plot(
        cp_range,
        carbon_v,
        color="#7c3aed",
        linewidth=2.5,
        label="Carbon market value"
    )
    ax2.fill_between(cp_range, 0, carbon_v, color="#7c3aed", alpha=0.10)
    ax2.plot(
        cp_range,
        carbon_v + fuel_save,
        color="#16a34a",
        linewidth=2.0,
        linestyle="--",
        label="Total value (carbon + fuel savings)"
    )

    cp_a = result["carbon_price"]
    fsa = max(0.0, (srmc - lcoe_r) * subst * 1000 / 1e9)
    totv = co2_avd * 1e6 * cp_a / 1e9 + fsa

    ax2.axvline(
        cp_a,
        color="#d97706",
        linewidth=2.0,
        linestyle="--",
        label=f"Applied ${cp_a:.0f}/tCO₂e → ${totv:.2f}B/yr"
    )
    ax2.scatter([cp_a], [totv], s=90, color="#d97706", zorder=8)
    ax2.text(
        cp_a + 3,
        totv + 0.04,
        f"${totv:.2f}B",
        fontsize=8.5,
        color="#d97706",
        fontweight="bold"
    )

    mac_g = result["mac_global"]

    if 0 < mac_g < 250:
        ax2.axvline(
            mac_g,
            color="#b91c1c",
            linewidth=1.5,
            linestyle=":",
            label=f"Breakeven MAC=${mac_g:.1f}/tCO₂e"
        )

    ax2.set_xlabel("Carbon Price (USD/tCO₂e)", fontsize=10)
    ax2.set_ylabel("Annual Economic Value (B USD/yr)", fontsize=10)
    ax2.set_xlim(0, 250)
    ax2.legend(fontsize=8, loc="upper left", framealpha=0.9)
    ax2.grid(linestyle="--", alpha=0.35, color="#d1d5db")
    ax2.spines[["top", "right"]].set_visible(False)
    ax2.tick_params(labelsize=9)
    ax2.yaxis.set_major_formatter(
        mticker.FormatStrFormatter("$%.2f B")
    )

    fig.text(
        0.98,
        0.01,
        f"GeoWorld Framework | {date.today()}",
        ha="right",
        fontsize=7,
        color="#777"
    )

    styler.save(fig, out_path)
    logger.info("Substitution curves saved: %s", out_path.name)


# ── Figure 4: Carbon Intensity ────────────────────────────────────────────────

def plot_carbon_intensity(
    styler: GeoWorldStyler,
    ci_result: Dict[str, Any],
    renew_gwh: Dict[str, float],
    result: Dict[str, Any],
    country_name: str,
    out_path: Path,
    thermal_params: Dict[str, Dict[str, float]],
    renew_lifecycle_ef: Dict[str, float],
) -> None:
    """
    Generate carbon intensity comparison charts.

    Args:
        styler: GeoWorldStyler instance for consistent styling.
        ci_result: Carbon intensity results dictionary.
        renew_gwh: Dictionary of renewable generation by technology (GWh/yr).
        result: Phase 7 abatement results dictionary.
        country_name: Country name for title.
        out_path: Output file path.
        thermal_params: Dictionary of thermal technology parameters.
        renew_lifecycle_ef: Dictionary of renewable lifecycle emission factors.
    """
    fig, (ax_bar, ax_decomp) = plt.subplots(
        1,
        2,
        figsize=(14, 6),
        dpi=styler.dpi,
        gridspec_kw={"wspace": 0.38}
    )
    fig.patch.set_facecolor(styler.fig_bg)

    title_main = (
        f"Carbon Intensity of Electricity Generation — {country_name}"
    )
    styler.add_standard_title(fig, title_main, y_main=0.99)

    ax_bar.set_facecolor("#FAFAFA")
    ax_bar.set_title(
        "(A) Grid Carbon Intensity — Before vs After Transition\n"
        "Electricity sector only · IEA Benchmarks (gCO₂eq/kWh)\n"
        "'Before' = thermal fleet modelled here "
        "(excludes existing hydro/nuclear)",
        fontsize=9,
        fontweight="bold"
    )

    categories = [
        "Thermal\n(before)",
        "Post-\ntransition",
        "Renewables\n(lifecycle avg)"
    ]
    values = [
        ci_result["ci_before_g_kwh"],
        ci_result["ci_after_g_kwh"],
        ci_result["ci_renew_avg_g_kwh"]
    ]
    colors_bar = ["#b91c1c", "#d97706", "#15803d"]

    bars_b = ax_bar.bar(
        categories,
        values,
        color=colors_bar,
        alpha=0.82,
        edgecolor="white",
        linewidth=1.5,
        width=0.55,
        zorder=4
    )

    for bar, val in zip(bars_b, values):
        ax_bar.text(
            bar.get_x() + bar.get_width() / 2,
            val + 3,
            f"{val:.0f}",
            ha="center",
            va="bottom",
            fontsize=11,
            fontweight="bold",
            color=bar.get_facecolor(),
            zorder=5
        )

    for lbl, val, col, ls in [
        (
            "World avg (IEA 2022)",
            ci_result["benchmark_world_g_kwh"],
            "#6b7280",
            "--"
        ),
        (
            "EU avg (IEA 2022)",
            ci_result["benchmark_eu_g_kwh"],
            "#7c3aed",
            "-."
        ),
        (
            "Net Zero 2050 (IEA)",
            ci_result["benchmark_netzero_g_kwh"],
            "#15803d",
            ":"
        ),
    ]:
        ax_bar.axhline(
            val,
            color=col,
            linewidth=1.8,
            linestyle=ls,
            alpha=0.9,
            zorder=3,
            label=f"{lbl}: {val:.0f} gCO₂/kWh"
        )

    red = ci_result["ci_reduction_pct"]
    ax_bar.annotate(
        f"−{red:.1f}%\nreduction",
        xy=(1, values[1]),
        xytext=(1.5, (values[0] + values[1]) / 2),
        fontsize=9,
        color="#15803d",
        fontweight="bold",
        arrowprops=dict(arrowstyle="->", color="#15803d", lw=1.5)
    )

    ax_bar.set_ylabel("Carbon Intensity (gCO₂eq/kWh)", fontsize=10)
    ax_bar.legend(fontsize=8, loc="upper right", framealpha=0.9)
    ax_bar.spines[["top", "right"]].set_visible(False)
    ax_bar.tick_params(labelsize=9)
    ax_bar.set_ylim(0, max(max(values), 1.0) * 1.35)

    ax_decomp.set_facecolor("#FAFAFA")
    ax_decomp.set_title(
        "(B) Lifecycle Emission Factor by Technology\n"
        "(IPCC AR5 WG3, Tab. A.II.4)",
        fontsize=10,
        fontweight="bold"
    )

    tech_items = [
        (
            "Coal (thermal)",
            thermal_params.get("coal", {}).get("ef", 820.0),
            "#7f1d1d"
        ),
        (
            "Gas CCGT (thermal)",
            thermal_params.get("gas", {}).get("ef", 490.0),
            "#c2410c"
        ),
        (
            "Oil (thermal)",
            thermal_params.get("oil", {}).get("ef", 750.0),
            "#78350f"
        ),
        (
            "Biomass",
            renew_lifecycle_ef.get("biomass", 230.0),
            "#15803d"
        ),
        (
            "Solar PV",
            renew_lifecycle_ef.get("solar", 48.0),
            "#d97706"
        ),
        (
            "Wind Onshore",
            renew_lifecycle_ef.get("wind", 11.0),
            "#1d4ed8"
        ),
    ]

    vals_t = [t[1] for t in tech_items]
    cols_t = [t[2] for t in tech_items]
    y_pos = np.arange(len(tech_items))

    hbars = ax_decomp.barh(
        y_pos,
        vals_t,
        color=cols_t,
        alpha=0.82,
        edgecolor="white",
        linewidth=1.2,
        height=0.55
    )

    for bar, val in zip(hbars, vals_t):
        if val > max(vals_t) * 0.7:
            ax_decomp.text(
                val - 50,
                bar.get_y() + bar.get_height() / 2,
                f"{val:.0f} gCO₂/kWh",
                va="center",
                fontsize=8.5,
                color="white",
                fontweight="bold"
            )
        else:
            ax_decomp.text(
                val + 8,
                bar.get_y() + bar.get_height() / 2,
                f"{val:.0f} gCO₂/kWh",
                va="center",
                fontsize=8.5,
                color=bar.get_facecolor(),
                fontweight="bold"
            )

    ax_decomp.set_yticks(y_pos)
    ax_decomp.set_yticklabels([t[0] for t in tech_items], fontsize=9)
    ax_decomp.set_xlabel(
        "Lifecycle GHG Emission Factor (gCO₂eq/kWh)",
        fontsize=9
    )
    ax_decomp.spines[["top", "right"]].set_visible(False)
    ax_decomp.tick_params(labelsize=9)
    ax_decomp.set_xlim(0, max(vals_t) * 1.22)
    ax_decomp.axhline(
        2.5,
        color="#888",
        linewidth=1.2,
        linestyle="--",
        alpha=0.6
    )
    ax_decomp.text(
        max(vals_t) * 0.5,
        2.65,
        "← Renewable  |  Thermal →",
        ha="center",
        fontsize=7.5,
        color="#888"
    )

    fig.text(
        0.98,
        0.01,
        f"GeoWorld Framework | {date.today()}",
        ha="right",
        fontsize=7,
        color="#777"
    )

    styler.save(fig, out_path)
    logger.info("Carbon intensity chart saved: %s", out_path.name)


# ── Figure 5: Net Zero Waterfall ──────────────────────────────────────────────

def plot_net_zero(
    styler: GeoWorldStyler,
    nz: Dict[str, Any],
    ci_result: Dict[str, Any],
    footprint: Dict[str, Any],
    result: Dict[str, Any],
    country_name: str,
    out_path: Path,
) -> None:
    """
    Generate Net Zero Analysis with 4 panels and two explicit scopes.

    (A) Waterfall: national CO₂ fossils balance
    (B) Electricity sector coverage gauge
    (C) Scope donut: post-transition GHG Protocol
    (D) NDC gap bar — national perspective

    Args:
        styler: GeoWorldStyler instance for consistent styling.
        nz: Net zero analysis results dictionary.
        ci_result: Carbon intensity results dictionary.
        footprint: Carbon footprint results dictionary.
        result: Phase 7 abatement results dictionary.
        country_name: Country name for title.
        out_path: Output file path.
    """
    nz_yr = nz.get("net_zero_year", 2050)
    el_cov = nz.get("elec_coverage_pct", 0.0)
    nat_c = nz.get("national_contribution_pct", 0.0)
    cov_ndc = nz.get("coverage_pct", np.nan)
    owid_warn = nz.get("owid_scope_warning", False)
    fossil_th = nz.get(
        "fossil_thermal_mt",
        result.get("total_thermal_co2", 0.0)
    )
    net_av = nz.get("net_avoided_mt", 0.0)
    tot_now = nz.get("current_total_mt", 0.0)
    tot_after = nz.get("total_after_mt", 0.0)
    target = nz.get("target_ndc_mt", np.nan)
    ndc_pct = nz.get("ndc_pct")
    ndc_yr = nz.get("ndc_horizon_year", 2030)
    base_yr = nz.get("base_year")

    fig = plt.figure(figsize=(18, 9.0), dpi=styler.dpi)
    fig.patch.set_facecolor(styler.fig_bg)

    title_main = (
        f"Net Zero Analysis — {country_name} | "
        f"Net-zero target: {nz_yr}"
    )
    styler.add_standard_title(fig, title_main, y_main=0.99)

    ax_wf = fig.add_axes([0.05, 0.14, 0.36, 0.76])
    ax_elec = fig.add_axes([0.46, 0.50, 0.22, 0.40])
    ax_sc = fig.add_axes([0.73, 0.50, 0.24, 0.40])
    ax_gap = fig.add_axes([0.46, 0.10, 0.51, 0.34])

    for ax in [ax_wf, ax_elec, ax_sc, ax_gap]:
        ax.set_facecolor("#FAFAFA")

    # ── (A) Waterfall ─────────────────────────────────────────────────────────
    ax_wf.set_title(
        "(A) Emission Balance — National Fossil CO₂ + Electricity Transition\n"
        "Fossil CO₂ only (excludes LULUCF / land-use change)",
        fontsize=8.5,
        fontweight="bold",
        pad=8,
    )

    wf_items = [
        ("Current\ntotal", tot_now, "+", "#b91c1c"),
        ("Power sector\n(modelled)", -fossil_th, "-", "#ea580c"),
        ("Other sectors\n(fossils)", -(tot_now - fossil_th), "-", "#6b7280"),
        (
            "CO₂ avoided\n(elec. trans)",
            -result["co2_avoided_mt"],
            "−",
            "#15803d"
        ),
        (
            "Lifecycle CO₂\n(renewables)",
            nz["renew_lifecycle_mt"],
            "+",
            "#d97706"
        ),
        (
            "Residual\nthermal CO₂",
            ci_result["residual_thermal_co2_mt"],
            "+",
            "#c2410c"
        ),
        ("Post-transition\ntotal", tot_after, "=", "#1d4ed8"),
    ]

    running = 0.0
    bar_pos = []

    for i, (lbl, val, typ, col) in enumerate(wf_items):
        if typ in ["+", "−", "-"]:
            bottom = min(running, running + val)
            height = abs(val)
            bar_pos.append((i, bottom, height, col, val, lbl, running))
            running += val
        else:
            bar_pos.append((i, 0, val, col, val, lbl, running))

    for x, bottom, height, color, val, lbl, start in bar_pos:
        ax_wf.bar(
            x,
            height,
            bottom=bottom,
            color=color,
            alpha=0.82,
            edgecolor="white",
            linewidth=1.2,
            width=0.65,
            zorder=3
        )

        if x < len(bar_pos) - 1 and val != 0:
            ax_wf.plot(
                [x + 0.325, x + 0.675],
                [start + val, start + val],
                color="#6b7280",
                lw=1.2,
                ls="--",
                alpha=0.6,
                zorder=2
            )

        is_last = x == len(bar_pos) - 1
        inside = abs(val) > max(tot_now * 0.06, 5) and val < 0

        if inside:
            y_t = bottom + height / 2
        else:
            if val > 0:
                y_t = bottom + height + tot_now * 0.01
            else:
                y_t = bottom - tot_now * 0.01

        va = "center" if inside else ("bottom" if val > 0 else "top")
        tc = "white" if inside else color

        bbox_dict = None
        if is_last:
            bbox_dict = dict(
                boxstyle="round,pad=0.2",
                fc="white",
                alpha=0.9,
                ec="none"
            )

        ax_wf.text(
            x,
            y_t,
            f"{val:+.1f} Mt" if not is_last else f"{val:.1f} Mt",
            ha="center",
            va=va,
            fontsize=7.5,
            color=tc,
            fontweight="bold",
            zorder=5,
            bbox=bbox_dict
        )

    if pd.notna(target):
        if ndc_pct and base_yr:
            lbl_t = (
                f"NDC {ndc_yr}: {target:.0f} Mt "
                f"(−{ndc_pct:.0f}% vs {base_yr})"
            )
        else:
            lbl_t = f"NDC {ndc_yr}: {target:.0f} Mt"

        ax_wf.axhline(
            target,
            color="#7c3aed",
            lw=2.0,
            ls="--",
            alpha=0.8,
            label=lbl_t,
            zorder=1
        )
        ax_wf.legend(fontsize=7, loc="upper right", framealpha=0.9)

    ax_wf.axhline(0, color="#111827", lw=1.2, alpha=0.4, zorder=2)
    ax_wf.set_xticks(range(len(wf_items)))
    ax_wf.set_xticklabels(
        [x[0] for x in wf_items],
        fontsize=8,
        rotation=35,
        ha="right",
        rotation_mode="anchor"
    )
    ax_wf.set_ylabel("CO₂ Emissions (MtCO₂e/yr)", fontsize=9)

    all_y = [p[1] + p[2] for p in bar_pos] + [p[1] for p in bar_pos]
    margin = (max(all_y) - min(all_y)) * 0.14
    ax_wf.set_ylim(min(all_y) - margin, max(all_y) + margin * 1.6)
    ax_wf.spines[["top", "right"]].set_visible(False)
    ax_wf.grid(axis="y", ls="--", alpha=0.25)
    ax_wf.tick_params(labelsize=8)

    # ── (B) Electricity Sector Coverage gauge ─────────────────────────────────
    ax_elec.set_title(
        "(B) Electricity Sector Coverage\n",
        fontsize=8.5,
        fontweight="bold",
        pad=4,
    )
    ax_elec.axis("off")
    ax_elec.set_xlim(0, 1)
    ax_elec.set_ylim(0, 1)

    cx = 0.50
    cy = 0.26
    r = 0.40

    ax_elec.text(
        0.50,
        0.95,
        f"Net CO₂ avoided:   {net_av:.2f} MtCO₂/yr",
        ha="center",
        va="top",
        fontsize=8.5,
        fontweight="bold",
        color="#15803d",
        transform=ax_elec.transAxes
    )
    ax_elec.text(
        0.50,
        0.85,
        f"Thermal fleet total:  {fossil_th:.2f} MtCO₂/yr",
        ha="center",
        va="top",
        fontsize=8.5,
        fontweight="bold",
        color="#374151",
        transform=ax_elec.transAxes
    )
    ax_elec.text(
        0.50,
        0.75,
        f"National fossils contribution: {nat_c:.1f}%",
        ha="center",
        va="top",
        fontsize=7.5,
        color="#6B7280",
        transform=ax_elec.transAxes
    )

    ax_elec.add_patch(
        mpatches.Wedge(
            (cx, cy),
            r,
            0,
            180,
            width=0.11,
            facecolor="#E5E7EB",
            edgecolor="white",
            lw=1.5,
            transform=ax_elec.transAxes
        )
    )

    if el_cov >= 60:
        col_gauge = "#16a34a"
    elif el_cov >= 30:
        col_gauge = "#d97706"
    else:
        col_gauge = "#dc2626"

    ax_elec.add_patch(
        mpatches.Wedge(
            (cx, cy),
            r,
            0,
            min(el_cov, 100) / 100.0 * 180,
            width=0.11,
            facecolor=col_gauge,
            edgecolor="white",
            lw=1.5,
            alpha=0.88,
            transform=ax_elec.transAxes
        )
    )

    ax_elec.text(
        cx,
        cy + 0.05,
        f"{el_cov:.1f}%",
        ha="center",
        va="center",
        fontsize=28,
        fontweight="bold",
        color=col_gauge,
        transform=ax_elec.transAxes
    )
    ax_elec.text(
        cx,
        cy - 0.06,
        "of thermal fleet CO₂\nsubstituted by renewables",
        ha="center",
        va="center",
        fontsize=8,
        color="#374151",
        transform=ax_elec.transAxes
    )

    # ── (C) Scope donut ───────────────────────────────────────────────────────
    ax_sc.set_title(
        "(C) Carbon Footprint by Scope\n(GHG Protocol — post-transition)",
        fontsize=8.5,
        fontweight="bold",
        pad=4
    )

    s1 = footprint.get("scope1_after_mt", 0.0)
    s2 = footprint.get("scope2_after_mt", 0.0)
    s3 = footprint.get("scope3_renew_mt", 0.0)
    total_s = s1 + s2 + s3

    if total_s > 0:
        wedges, texts, autotexts = ax_sc.pie(
            [s1, s2, s3],
            labels=["Scope 1\n(Direct)", "Scope 2\n(Grid)", "Scope 3\n(Renew)"],
            colors=["#b91c1c", "#ea580c", "#15803d"],
            autopct=lambda p: f"{p:.1f}%\n({p / 100 * total_s:.1f} Mt)",
            startangle=90,
            pctdistance=0.68,
            textprops={"fontsize": 7.5},
            wedgeprops={"edgecolor": "white", "linewidth": 1.5},
        )

        for at in autotexts:
            at.set_fontsize(7)
            at.set_fontweight("bold")

        ax_sc.add_patch(plt.Circle((0, 0), 0.55, fc="white"))
        ax_sc.text(
            0,
            0,
            f"{total_s:.1f}\nMtCO₂e/yr",
            ha="center",
            va="center",
            fontsize=9,
            fontweight="bold",
            color="#374151"
        )

    # ── (D) NDC Gap Analysis ──────────────────────────────────────────────────
    ax_gap.set_title(
        f"(D) NDC {ndc_yr} Gap Analysis — National Perspective\n"
        + (
            "OWID/WB with fossil CO₂ only (excludes LULUCF)"
            if owid_warn
            else "Fossil CO₂ vs NDC target "
            "(note: NDC covers total CO₂e incl. LULUCF)"
        ),
        fontsize=8.5,
        fontweight="bold",
        pad=4,
    )

    gap_items = [
        ("Current (fossil CO₂)", tot_now, "#b91c1c"),
        ("Post-transition", tot_after, "#d97706")
    ]

    if pd.notna(target):
        gap_items.append(
            (f"NDC {ndc_yr} target (CO₂e total)", target, "#7c3aed")
        )

    y_labels = []

    for lbl, val, _ in gap_items:
        if "target" in lbl.lower():
            y_labels.append(f"NDC {ndc_yr} target (CO₂e)")
        elif "Current" in lbl:
            y_labels.append("Current (fossil CO₂)")
        else:
            y_labels.append("Post-transition")

    x_vals = [v for _, v, _ in gap_items]
    g_colors = [c for _, _, c in gap_items]
    y_pos = np.arange(len(gap_items))

    ax_gap.barh(
        y_pos,
        x_vals,
        color=g_colors,
        alpha=0.82,
        edgecolor="white",
        linewidth=1.2,
        height=0.40
    )

    ax_gap.set_yticks([])

    for i, (v, c, lbl) in enumerate(zip(x_vals, g_colors, y_labels)):
        ax_gap.text(
            max(x_vals) * 0.01,
            i + 0.25,
            lbl,
            va="bottom",
            ha="left",
            fontsize=8.5,
            color="#374151",
            fontweight="bold"
        )

        ax_gap.text(
            v + max(x_vals) * 0.015,
            i,
            f"{v:.1f} Mt",
            va="center",
            ha="left",
            fontsize=9,
            fontweight="bold",
            color=c
        )

    if owid_warn:
        ann = (
            f"Elec. sector coverage: {el_cov:.1f}%  |  "
            f"National fossils contribution: {nat_c:.1f}%  |  "
            "NDC gap: N/A (OWID fossils-only)"
        )
    elif pd.notna(cov_ndc) and nz.get("current_gap_mt", 0) > 1:
        resid = nz.get("residual_gap_mt", 0.0)
        ann = (
            f"NDC gap coverage: {cov_ndc:.1f}%  |  "
            f"Residual: {resid:.1f} Mt  |  "
            f"Elec. sector coverage: {el_cov:.1f}%"
        )
    else:
        ann = (
            f"Elec. sector coverage: {el_cov:.1f}%  |  "
            f"National fossils contribution: {nat_c:.1f}%"
        )

    ax_gap.set_xlabel(
        f"CO₂ Emissions (MtCO₂e/yr)        {ann}",
        fontsize=8
    )

    ax_gap.set_xlim(0, max(x_vals) * 1.30 if x_vals else 100)

    ax_gap.spines[["top", "right", "left"]].set_visible(False)
    ax_gap.tick_params(axis="x", labelsize=8)

    # ── Footnotes ─────────────────────────────────────────────────────────────
    fig.text(
        0.05,
        0.025,
        "OWID/World Bank reports fossil CO₂ only. "
        "For high-LULUCF countries (e.g. Brazil), "
        "total CO₂e incl. land-use is substantially higher.",
        fontsize=7,
        color="#7C3AED",
        style="italic"
    )
    fig.text(
        0.98,
        0.015,
        f"NDC source: {nz.get('source', '—')}  |  "
        f"GeoWorld Framework | {date.today()}",
        ha="right",
        fontsize=7,
        color="#9CA3AF"
    )

    styler.save(fig, out_path)
    logger.info("Net Zero analysis saved: %s", out_path.name)