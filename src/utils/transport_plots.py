"""
src/utils/transport_plots.py
==============================
Visualizations for Phase 9 (Transport Decarbonisation).

Separated from TransportDecarbonizationCalculator to keep the main module
lean, mirroring the pattern already established by abatement_plots.py and
sensitivity_plots.py. Each function receives a GeoWorldStyler and the
necessary data explicitly — no global state, no dependency on the
caller's instance attributes.
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

import geopandas as gpd
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

try:
    import rasterio
    _HAS_RASTERIO = True
except ImportError:
    _HAS_RASTERIO = False

from src.utils.map_styling import GeoWorldStyler

logger = logging.getLogger("geoworld.utils.transport_plots")

# ── Visual palette ──────────────────────────────────────────────────────────
POWERTRAIN_COLORS = {
    "ice":  "#C62828",
    "hev":  "#F9A825",
    "phev": "#FDD835",
    "bev":  "#1565C0",
    "fcev": "#00ACC1",
}
SCENARIO_COLORS = {
    "reference":    "#1565C0",
    "accelerated":  "#1B5E20",
    "conservative": "#B71C1C",
}
TECH_RE_COLORS = {
    "solar":   "#F9A825",
    "wind":    "#1565C0",
    "biomass": "#6A1B9A",
}


# ── Figure 1: Emission trajectories ───────────────────────────────────────────

def plot_emissions_trajectory(
    styler:       GeoWorldStyler,
    ts_df:        pd.DataFrame,
    country_name: str,
    code:         str,
    out_path:     Path,
) -> None:
    fig, axes = plt.subplots(1, 2, figsize=(14, 6), dpi=styler.dpi)
    fig.patch.set_facecolor(styler.fig_bg)
    fig.suptitle(
        f"Transport GHG Emission Trajectories — {country_name}\n"
        "ICE · HEV · PHEV · BEV · FCEV — 2025–2050",
        fontsize=12, fontweight="bold",
    )
    for sc, color in SCENARIO_COLORS.items():
        sub = ts_df[ts_df.scenario == sc].sort_values("year")
        if sub.empty:
            continue
        if "co2_total_mtco2eq" in sub.columns:
            axes[0].plot(
                sub["year"], sub["co2_total_mtco2eq"],
                color=color, label=sc.capitalize(), linewidth=2,
            )
            axes[0].plot(
                sub["year"], sub["co2_baseline_mtco2eq"],
                color=color, linestyle="--", alpha=0.35, linewidth=1.2,
            )
        if "co2_avoided_mtco2eq" in sub.columns:
            axes[1].fill_between(
                sub["year"], 0, sub["co2_avoided_mtco2eq"],
                color=color, alpha=0.35, label=sc.capitalize(),
            )
            axes[1].plot(
                sub["year"], sub["co2_avoided_mtco2eq"],
                color=color, linewidth=2,
            )

    for ax, title, ylabel in [
        (
            axes[0],
            "Total Transport Emissions vs. Full-ICE Baseline",
            "MtCO2eq/yr",
        ),
        (
            axes[1],
            "Annual GHG Abatement (vs. Full-ICE Counterfactual)",
            "MtCO2eq/yr avoided",
        ),
    ]:
        ax.set_title(title, fontsize=10, fontweight="bold")
        ax.set_ylabel(ylabel, fontsize=9)
        ax.set_xlabel("Year", fontsize=9)
        ax.legend(fontsize=8)
        ax.yaxis.grid(True, alpha=0.3)
        ax.set_facecolor("#FAFAFA")
        ax.spines[["top", "right"]].set_visible(False)

    axes[0].text(
        0.97, 0.97, "Dashed = Full-ICE baseline",
        transform=axes[0].transAxes, ha="right", va="top",
        fontsize=7, color="#888888",
    )
    plt.tight_layout(rect=[0, 0, 1, 0.93])
    styler.save(fig, out_path)


# ── Figure 2: Fleet powertrain transition ─────────────────────────────────────

def plot_fleet_transition(
    styler:       GeoWorldStyler,
    fleet_df:     pd.DataFrame,
    country_name: str,
    code:         str,
    out_path:     Path,
) -> None:
    import matplotlib.patheffects as pe

    sub = fleet_df[fleet_df.scenario == "reference"].copy()
    if sub.empty:
        return

    agg = sub.groupby("year")[
        ["total_fleet", "stock_ice", "stock_hev",
         "stock_phev", "stock_bev", "stock_fcev"]
    ].sum().reset_index()

    total = agg["total_fleet"].clip(lower=1)
    pct   = {
        "ice":  100 * agg["stock_ice"]  / total,
        "hev":  100 * agg["stock_hev"]  / total,
        "phev": 100 * agg["stock_phev"] / total,
        "bev":  100 * agg["stock_bev"]  / total,
        "fcev": 100 * agg["stock_fcev"] / total,
    }

    fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(15, 6), dpi=styler.dpi)
    fig.patch.set_facecolor(styler.fig_bg)
    fig.suptitle(
        f"Light-Vehicle Fleet Powertrain Transition — {country_name}",
        fontsize=12, fontweight="bold",
    )

    pt_order  = ("ice", "hev", "phev", "bev", "fcev")
    pt_labels = {
        "ice": "ICE", "hev": "HEV", "phev": "PHEV",
        "bev": "BEV", "fcev": "FCEV",
    }
    outline_fx = [pe.withStroke(linewidth=2.5, foreground="black")]

    ax1.stackplot(
        agg["year"],
        *[pct[pt] for pt in pt_order],
        labels=[pt_labels[pt] for pt in pt_order],
        colors=[POWERTRAIN_COLORS[pt] for pt in pt_order],
        alpha=0.88,
    )
    ax1.set_ylim(0, 100)
    ax1.set_xlim(agg["year"].min(), agg["year"].max())
    ax1.set_title(
        "Fleet Composition by Powertrain — Reference (%)",
        fontsize=10, fontweight="bold",
    )
    ax1.set_ylabel("Share of total fleet (%)", fontsize=9)
    ax1.set_xlabel("Year", fontsize=9)
    ax1.legend(fontsize=8, loc="upper right", framealpha=0.85)
    ax1.yaxis.grid(True, alpha=0.3)
    ax1.set_facecolor("#FAFAFA")
    ax1.spines[["top", "right"]].set_visible(False)

    milestones = [
        y for y in (2025, 2030, 2035, 2040, 2045, 2050)
        if y in agg["year"].values
    ]
    for y in milestones:
        row        = agg[agg["year"] == y].iloc[0]
        row_total  = max(float(row["total_fleet"]), 1)
        cumulative = 0.0
        for pt in pt_order:
            share = 100 * float(row[f"stock_{pt}"]) / row_total
            mid   = cumulative + share / 2
            if share >= 5.0:
                text_y = max(3.0, min(97.0, mid))
                ax1.text(
                    y, text_y, f"{share:.0f}%",
                    ha="center", va="center",
                    fontsize=6.5, color="white", fontweight="bold",
                    path_effects=outline_fx, clip_on=True,
                )
            cumulative += share

    fig.delaxes(ax2)
    gs_right = fig.add_gridspec(
        3, 1, left=0.52, right=0.97, top=0.88, bottom=0.10, hspace=0.55
    )
    scenario_axes = [fig.add_subplot(gs_right[i]) for i in range(3)]
    scenarios     = ("reference", "accelerated", "conservative")
    _SC_TITLES    = {
        "reference":    "Reference",
        "accelerated":  "Accelerated",
        "conservative": "Conservative",
    }

    for ax_s, sc in zip(scenario_axes, scenarios):
        sc_sub = fleet_df[fleet_df.scenario == sc].groupby("year")[
            ["total_fleet", "stock_ice", "stock_hev",
             "stock_phev", "stock_bev", "stock_fcev"]
        ].sum().reset_index()

        sc_total = sc_sub["total_fleet"].clip(lower=1)
        sc_pct   = {
            pt: 100 * sc_sub[f"stock_{pt}"] / sc_total
            for pt in pt_order
        }

        ax_s.stackplot(
            sc_sub["year"],
            *[sc_pct[pt] for pt in pt_order],
            labels=[pt_labels[pt] for pt in pt_order],
            colors=[POWERTRAIN_COLORS[pt] for pt in pt_order],
            alpha=0.88,
        )
        ax_s.set_ylim(0, 100)
        ax_s.set_xlim(sc_sub["year"].min(), sc_sub["year"].max())
        ax_s.set_title(
            _SC_TITLES[sc], fontsize=9, fontweight="bold",
            color=SCENARIO_COLORS[sc],
        )
        ax_s.set_ylabel("Share (%)", fontsize=7)
        ax_s.yaxis.grid(True, alpha=0.25)
        ax_s.set_facecolor("#FAFAFA")
        ax_s.spines[["top", "right"]].set_visible(False)
        ax_s.tick_params(axis="both", labelsize=7)

        if sc == "conservative":
            ax_s.set_xlabel("Year", fontsize=8)
        else:
            ax_s.set_xticklabels([])

        last = sc_sub[sc_sub.year == sc_sub.year.max()]
        if not last.empty:
            last_total   = max(float(last["total_fleet"].values[0]), 1)
            bev_pct_2050 = 100 * float(last["stock_bev"].values[0]) / last_total
            ice_p        = 100 * float(last["stock_ice"].values[0])  / last_total
            hev_p        = 100 * float(last["stock_hev"].values[0])  / last_total
            phev_p       = 100 * float(last["stock_phev"].values[0]) / last_total
            bev_mid      = ice_p + hev_p + phev_p + bev_pct_2050 / 2
            text_y       = max(3.0, min(97.0, bev_mid))
            ax_s.text(
                sc_sub["year"].max(), text_y,
                f"BEV: {bev_pct_2050:.0f}%",
                ha="right", va="center",
                fontsize=6.5, color="white", fontweight="bold",
                path_effects=outline_fx, clip_on=True,
            )

    handles, labels_leg = scenario_axes[0].get_legend_handles_labels()
    scenario_axes[0].legend(
        handles, labels_leg, fontsize=6.5, loc="upper right",
        framealpha=0.85, ncol=5,
    )

    styler.save(fig, out_path)


# ── Figure 3: Renewable energy requirements ───────────────────────────────────

def plot_renewable_need(
    styler:       GeoWorldStyler,
    ts_df:        pd.DataFrame,
    country_name: str,
    code:         str,
    out_path:     Path,
) -> None:
    fig, axes = plt.subplots(2, 2, figsize=(14, 10), dpi=styler.dpi)
    fig.patch.set_facecolor(styler.fig_bg)
    fig.suptitle(
        f"Renewable Energy Requirements for Transport Electrification"
        f" — {country_name}",
        fontsize=12, fontweight="bold",
    )

    re_avail = {
        "solar": (
            float(ts_df["re_avail_solar_gw"].iloc[0])
            if "re_avail_solar_gw" in ts_df.columns else 0.0
        ),
        "wind": (
            float(ts_df["re_avail_wind_gw"].iloc[0])
            if "re_avail_wind_gw" in ts_df.columns else 0.0
        ),
    }

    for sc, color in SCENARIO_COLORS.items():
        sub = ts_df[ts_df.scenario == sc].sort_values("year")
        if sub.empty:
            continue
        label = sc.capitalize()

        axes[0, 0].plot(
            sub["year"], sub["ev_electricity_demand_twh"],
            color=color, label=label, linewidth=2,
        )
        axes[0, 1].plot(
            sub["year"], sub["re_needed_total_gw"],
            color=color, label=label, linewidth=2,
        )
        if "annual_capex_bn_usd" in sub.columns:
            axes[1, 0].plot(
                sub["year"], sub["annual_capex_bn_usd"],
                color=color, label=label, linewidth=2,
            )
        if "abatement_cost_usd_tco2" in sub.columns:
            abat = sub["abatement_cost_usd_tco2"]
            axes[1, 1].plot(
                sub["year"], abat, color=color, label=label, linewidth=2,
            )
            axes[1, 1].fill_between(
                sub["year"], abat, 0,
                where=(abat < 0), color=color, alpha=0.15,
            )

    for tech, clr in [
        ("solar", TECH_RE_COLORS["solar"]),
        ("wind",  TECH_RE_COLORS["wind"]),
    ]:
        if re_avail[tech] > 0:
            axes[0, 1].axhline(
                re_avail[tech], color=clr, linestyle=":", linewidth=1.5,
                label=f"{tech.capitalize()} potential ({re_avail[tech]:.0f} GW)",
            )

    titles  = [
        "BEV+PHEV Electricity Demand",
        "Required RE Capacity (Cumulative)",
        "Annual RE Investment Required",
        "Abatement Cost (Net capex / CO₂ avoided)",
    ]
    ylabels = ["TWh/yr", "GW installed", "USD Billion/yr", "USD/tCO₂ avoided"]

    for ax, title, ylabel in zip(axes.flat, titles, ylabels):
        ax.set_title(title, fontsize=10, fontweight="bold")
        ax.set_ylabel(ylabel, fontsize=9)
        ax.set_xlabel("Year", fontsize=9)
        ax.legend(fontsize=7)
        ax.yaxis.grid(True, alpha=0.3)
        ax.set_facecolor("#FAFAFA")
        ax.spines[["top", "right"]].set_visible(False)

    axes[1, 1].axhline(0, color="#888888", linewidth=0.8, linestyle="--")
    axes[1, 1].set_ylim(
        min(-5, axes[1, 1].get_ylim()[0]),
        max(5,  axes[1, 1].get_ylim()[1]),
    )
    axes[0, 1].legend(fontsize=6)

    plt.tight_layout(rect=[0, 0, 1, 0.95])
    styler.save(fig, out_path)


# ── Figure 4: EV charging hub network map ─────────────────────────────────────

def plot_hub_map(
    styler:          GeoWorldStyler,
    admin_gdf:       Optional[gpd.GeoDataFrame],
    hubs_gdf:        gpd.GeoDataFrame,
    mainland_gdf:    gpd.GeoDataFrame,
    context_gdf:     Optional[gpd.GeoDataFrame],
    country_name:    str,
    code:            str,
    suitability_dir: Optional[Path],
    out_path:        Path,
) -> None:
    import matplotlib.patches as mpatches

    _HUB_SOURCE_COLORS = {
        "Solar":   "#C62828",
        "Wind":    "#0D47A1",
        "Biomass": "#4A148C",
    }
    _HUB_SOURCE_EDGE = {
        "Solar":   "#FFCDD2",
        "Wind":    "#BBDEFB",
        "Biomass": "#E1BEE7",
    }

    suit_arr = transform = crs = extent = None
    if _HAS_RASTERIO and suitability_dir is not None:
        tif_cands = sorted(
            Path(suitability_dir).glob(f"{code}_solar_suitability*.tif")
        )
        if not tif_cands:
            tif_cands = sorted(Path(suitability_dir).glob("*.tif"))
        if tif_cands:
            try:
                with rasterio.open(tif_cands[0]) as src:
                    suit_arr  = src.read(1).astype(np.float32)
                    transform = src.transform
                    crs       = str(src.crs)
                    nd        = (
                        src.nodata if src.nodata is not None else -9999.0
                    )
                suit_arr[suit_arr == nd] = np.nan
                H, W    = suit_arr.shape
                extent  = [
                    transform.c,
                    transform.c + transform.a * W,
                    transform.f + transform.e * H,
                    transform.f,
                ]
            except Exception as exc:
                logger.debug("Could not load suitability raster: %s", exc)

    vb      = mainland_gdf.total_bounds
    fig, ax = styler.create_figure(
        vb[0], vb[2], vb[1], vb[3], right_in_override=1.80
    )
    styler.draw_basemap(
        ax, crs or "EPSG:4326", mainland_gdf, context_gdf,
        admin_gdf, extent=extent,
    )

    suitability_im = None
    if suit_arr is not None and extent is not None:
        cmap = styler.make_cmap(
            "YlOrRd", bad="none", under="#AAAAAA",
            vmin_frac=0.08, vmax_frac=1.0,
        )
        suitability_im = ax.imshow(
            np.where(
                np.isfinite(suit_arr) & (suit_arr > 0), suit_arr, np.nan
            ),
            extent=extent, origin="upper", cmap=cmap,
            vmin=0, vmax=1.0, alpha=0.50, zorder=2,
            interpolation="bilinear",
        )

    minx, miny, maxx, maxy = vb
    hubs_in = hubs_gdf[
        (hubs_gdf.geometry.x >= minx) & (hubs_gdf.geometry.x <= maxx)
        & (hubs_gdf.geometry.y >= miny) & (hubs_gdf.geometry.y <= maxy)
    ].copy()

    if not hubs_in.empty:
        if "primary_source" not in hubs_in.columns:
            hubs_in["primary_source"] = "Solar"
        for src_label, grp in hubs_in.groupby("primary_source"):
            clr   = _HUB_SOURCE_COLORS.get(src_label, "#888888")
            eclr  = _HUB_SOURCE_EDGE.get(src_label, "#444444")
            sizes = np.clip(grp["suitability_score"].values * 80, 12, 85)
            ax.scatter(
                grp.geometry.x, grp.geometry.y,
                s=sizes, c=clr, alpha=0.88,
                edgecolors=eclr, linewidth=0.7,
                label=f"{src_label} hubs (n={len(grp):,})",
                zorder=5,
            )

    styler.add_decorations(ax, vb[0], vb[2], vb[1], vb[3])
    if suitability_im is not None:
        styler.add_colorbar(
            fig, suitability_im, "Suitability Score (0–1)", extend="neither"
        )
    if admin_gdf is not None:
        styler.draw_admin_labels(
            ax, admin_gdf, vb[0], vb[2], vb[1], vb[3]
        )

    if not hubs_in.empty:
        hub_radius     = float(hubs_gdf["hub_radius_km"].iloc[0])
        legend_handles = []
        for src_label in sorted(hubs_in["primary_source"].unique()):
            n_src = (hubs_in["primary_source"] == src_label).sum()
            clr   = _HUB_SOURCE_COLORS.get(src_label, "#888888")
            eclr  = _HUB_SOURCE_EDGE.get(src_label, "#FFFFFF")
            legend_handles.append(
                mpatches.Patch(
                    facecolor=clr, edgecolor=eclr, linewidth=0.8,
                    label=f"{src_label} (n={n_src:,}, ≥{hub_radius:.0f} km)",
                )
            )
        ax.legend(
            handles=legend_handles,
            loc="lower center", bbox_to_anchor=(0.5, 0.01),
            ncol=min(3, len(legend_handles)),
            fontsize=8, framealpha=0.90, frameon=True,
            facecolor="white", edgecolor="#CCCCCC",
            borderpad=0.6, handlelength=1.2,
        )

    src_counts = (
        hubs_in["primary_source"].value_counts().to_dict()
        if not hubs_in.empty and "primary_source" in hubs_in.columns
        else {}
    )
    src_str = " | ".join(
        f"{k}: {v:,}" for k, v in sorted(src_counts.items())
    ) if src_counts else ""

    styler.add_standard_title(
        fig,
        title_main="EV Charging Hub Network — 2050 Horizon",
        title_sub=(
            f"{country_name}  |  Light Vehicles Only  |  "
            f"Coloured by RE Source  |  Sized by Suitability Score"
        ),
    )
    hub_stats = (
        f"Total hubs: {len(hubs_in):,}  |  {src_str}  |  "
        f"Mean suitability: {hubs_in['suitability_score'].mean():.3f}  |  "
        f"Hub spacing: ≥{hubs_gdf['hub_radius_km'].iloc[0]:.0f} km  |  "
        f"Total capex: USD "
        f"{len(hubs_in) * float(hubs_gdf['capex_usd'].iloc[0]) / 1e9:.2f}B"
    )
    styler.add_standard_footer(
        fig, stats_text=hub_stats, crs_metadata=f"CRS: {crs or 'EPSG:4326'}"
    )
    styler.save(fig, out_path)
