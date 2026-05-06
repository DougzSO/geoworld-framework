"""
map_styling.py — Central Styler and Map Layout Engine for the GeoWorld pipeline.
=================================================================================
Provides GeoWorldStyler, a unified class responsible for:
  - Figure and axes creation with cosine-corrected aspect ratios.
  - Basemap rendering (context countries, admin boundaries).
  - Standard titles, footers, legends, colorbars, and stats strips.
  - Compass rose and segmented scale bar decorations.
  - Multi-panel comparison layouts via PIL compositing.
  - Consistent save interface for all pipeline figures.

Usage:
    styler = GeoWorldStyler(settings_vis=cfg.vis, global_dpi=150)
    fig, ax = styler.create_figure(minx, maxx, miny, maxy)
    styler.draw_basemap(ax, "EPSG:4326", mainland_gdf)
    styler.add_decorations(ax, minx, maxx, miny, maxy)
    styler.save(fig, out_path)
"""

import io
import logging
import math
import unicodedata
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import geopandas as gpd
import matplotlib.colors as mcolors
import matplotlib.patheffects as patheffects
import matplotlib.pyplot as plt
import numpy as np

try:
    from PIL import Image as _PIL_Image
except ImportError:
    _PIL_Image = None

logger = logging.getLogger("geoworld.map_styling")


class GeoWorldStyler:
    """
    Central styler and map layout engine for the GeoWorld pipeline.

    Provides consistent figure sizing, basemap rendering, decorations,
    colorbars, legends, titles, footers, and multi-panel compositing
    for all pipeline visualization outputs.
    """

    # ── Layout buffers (fraction of data span) ──
    BUF_TOP = 0.095
    BUF_BOTTOM = 0.105
    BUF_SIDES = 0.065

    # ── Figure margins (inches) ──
    TOP_IN = 1.10
    BOT_IN = 2.0
    LEFT_IN = 0.80
    RIGHT_IN_CBAR = 1.55

    # ── Contour styles ──
    CONTOUR_P10_COLOR = "#15803D"
    CONTOUR_P10_LINEWIDTH = 1.5
    CONTOUR_P90_COLOR = "#C0392B"
    CONTOUR_P90_LINEWIDTH = 1.0

    # ── Font sizes (optimized for academic papers/theses) ──
    ADMIN_LABEL_FONTSIZE = 11.5
    STATS_STRIP_FONTSIZE = 16.0
    LEGEND_FONTSIZE = 14.0
    TITLE_FONTSIZE_MAIN = 22
    TITLE_FONTSIZE_SUB = 17
    FOOTER_FONTSIZE_PARAMS = 15
    FOOTER_FONTSIZE_STATS = 18
    AXES_LABEL_FONTSIZE = 16
    AXES_TICK_FONTSIZE = 14

    FIG_H_MAX = 24

    def __init__(self, settings_vis: Dict[str, Any], global_dpi: int = 150):
        """
        Initialize GeoWorldStyler.

        Args:
            settings_vis: Visualization settings dictionary (from config).
            global_dpi: Minimum DPI for all figures.
        """
        self.layout = settings_vis.get("layout", {})
        self.colors = settings_vis.get("colors", {})
        self.dpi = max(global_dpi, self.layout.get("dpi", 300))

        default_width = self.layout.get("figure_width_inches", 10.0)
        self.fig_width = max(default_width, 10.0)

        self.ocean_color = self.colors.get(
            "ocean_background", "#D6EAF8"
        )
        self.fig_bg = self.colors.get(
            "figure_background", "#F8F9FA"
        )
        self.ctx_fill = self.colors.get(
            "context_countries_fill", "#E8E8E8"
        )
        self.ctx_edge = self.colors.get(
            "context_countries_edge", "#B0B0B0"
        )
        self.country_border = self.colors.get(
            "country_border", "#1A1A1A"
        )

        self.BUF_TOP = self.layout.get(
            "buffer_top", self.__class__.BUF_TOP
        )
        self.BUF_BOTTOM = self.layout.get(
            "buffer_bottom", self.__class__.BUF_BOTTOM
        )
        self.BUF_SIDES = self.layout.get(
            "buffer_sides", self.__class__.BUF_SIDES
        )

    def axes_center_x(self, fig: plt.Figure) -> float:
        """
        Compute the horizontal center of the main map axes.

        Args:
            fig: Matplotlib figure.

        Returns:
            x-coordinate (figure fraction) of the map axes center.
        """
        map_axes = [
            a for a in fig.axes
            if not getattr(a, "_is_cbar_ax", False)
        ]

        if not map_axes:
            return 0.5

        pos = map_axes[0].get_position()
        return pos.x0 + pos.width / 2.0

    def make_cmap(
        self,
        name: str,
        reverse: bool = False,
        under: str = "none",
        bad: str = "none",
        lut: int = 256,
        vmin_frac: float = 0.0,
        vmax_frac: float = 1.0,
        colorblind_safe: bool = False
    ) -> mcolors.Colormap:
        """
        Build a Matplotlib colormap with optional subsetting and accessibility.

        Args:
            name: Colormap name (e.g., 'viridis', 'RdYlGn').
            reverse: If True, append '_r' for reversed map.
            under: Color for values below vmin.
            bad: Color for NaN/masked values.
            lut: Number of colormap levels.
            vmin_frac: Start fraction of the base colormap [0-1].
            vmax_frac: End fraction of the base colormap [0-1].
            colorblind_safe: If True, replace colorblind-unsafe maps.

        Returns:
            Configured Matplotlib Colormap.
        """
        if colorblind_safe:
            unsafe_maps = {
                "rdylgn": "YlGnBu",
                "piyg": "puor",
                "prgn": "puor",
                "red-green": "YlGnBu",
                "jet": "viridis",
            }
            base_name = name.lower()
            if base_name in unsafe_maps:
                name = unsafe_maps[base_name]

        full_name = name + "_r" if reverse else name
        base = plt.get_cmap(full_name)

        if vmin_frac != 0.0 or vmax_frac != 1.0:
            cmap = mcolors.LinearSegmentedColormap.from_list(
                f"gw_{full_name}",
                base(np.linspace(vmin_frac, vmax_frac, lut)),
                N=lut,
            )
        else:
            cmap = base.copy()

        cmap.set_under(under)
        cmap.set_bad(bad)
        return cmap

    def _ax_bounds(
        self,
        minx: float,
        maxx: float,
        miny: float,
        maxy: float,
        buf_top: Optional[float] = None,
        buf_bottom: Optional[float] = None,
        buf_sides: Optional[float] = None
    ) -> Tuple[float, float, float, float]:
        """
        Compute axes bounds with layout buffers applied.

        Args:
            minx: Minimum longitude of data.
            maxx: Maximum longitude of data.
            miny: Minimum latitude of data.
            maxy: Maximum latitude of data.
            buf_top: Override for top buffer fraction.
            buf_bottom: Override for bottom buffer fraction.
            buf_sides: Override for side buffer fraction.

        Returns:
            Tuple of (ax_minx, ax_maxx, ax_miny, ax_maxy).
        """
        lon_span = maxx - minx
        lat_span = maxy - miny

        bt = (
            buf_top if buf_top is not None else self.BUF_TOP
        ) * lat_span
        bb = (
            buf_bottom if buf_bottom is not None else self.BUF_BOTTOM
        ) * lat_span
        bs = (
            buf_sides if buf_sides is not None else self.BUF_SIDES
        ) * lon_span

        return minx - bs, maxx + bs, miny - bb, maxy + bt

    def create_figure(
        self,
        minx: float,
        maxx: float,
        miny: float,
        maxy: float,
        right_in_override: Optional[float] = None
    ) -> Tuple[plt.Figure, plt.Axes]:
        """
        Create a figure and axes sized for the given geographic extent.

        Applies cosine-corrected aspect ratio to match visual proportions.

        Args:
            minx: Minimum longitude of data.
            maxx: Maximum longitude of data.
            miny: Minimum latitude of data.
            maxy: Maximum latitude of data.
            right_in_override: Override for right margin (inches).

        Returns:
            Tuple of (Figure, Axes).
        """
        ax_minx, ax_maxx, ax_miny, ax_maxy = self._ax_bounds(
            minx, maxx, miny, maxy
        )
        lat_ax = ax_maxy - ax_miny
        lon_ax = ax_maxx - ax_minx

        mid_lat = (miny + maxy) / 2.0
        cos_lat = math.cos(math.radians(mid_lat))
        visual_aspect = (lat_ax / lon_ax) / cos_lat

        if visual_aspect < 0.85:
            map_width_in = max(self.fig_width, 13.5)
        elif visual_aspect < 1.10:
            map_width_in = max(self.fig_width, 11.5)
        else:
            map_width_in = max(self.fig_width, 10.0)

        map_height_in = map_width_in * visual_aspect
        max_h_allowed = self.FIG_H_MAX - (self.TOP_IN + self.BOT_IN)

        if map_height_in > max_h_allowed:
            map_height_in = max_h_allowed
            map_width_in = map_height_in / visual_aspect

        left_in = self.LEFT_IN
        right_in = (
            right_in_override
            if right_in_override is not None
            else self.RIGHT_IN_CBAR
        )

        fig_w = map_width_in + left_in + right_in
        fig_h = map_height_in + self.TOP_IN + self.BOT_IN

        fig = plt.figure(figsize=(fig_w, fig_h), dpi=self.dpi)
        fig.patch.set_facecolor(self.fig_bg)

        ax_l = left_in / fig_w
        ax_b = self.BOT_IN / fig_h
        ax_w = map_width_in / fig_w
        ax_h = map_height_in / fig_h

        ax = fig.add_axes([ax_l, ax_b, ax_w, ax_h])
        ax.set_facecolor(self.ocean_color)
        ax.set_xlim(ax_minx, ax_maxx)
        ax.set_ylim(ax_miny, ax_maxy)

        return fig, ax

    def draw_basemap(
        self,
        ax: plt.Axes,
        crs: str,
        mainland_gdf: gpd.GeoDataFrame,
        context_gdf: Optional[gpd.GeoDataFrame] = None,
        admin_gdf: Optional[gpd.GeoDataFrame] = None,
        border_linewidth: float = 1.5,
        extent: Optional[list] = None
    ) -> None:
        """
        Render basemap layers: context countries, country border, admin boundaries.

        Args:
            ax: Matplotlib axes.
            crs: Target CRS string (e.g., 'EPSG:4326').
            mainland_gdf: GeoDataFrame of the main country geometry.
            context_gdf: Optional GeoDataFrame of neighboring countries.
            admin_gdf: Optional GeoDataFrame of administrative boundaries.
            border_linewidth: Line width for country border.
            extent: Optional [x0, x1, y0, y1] to clip the view.
        """
        if context_gdf is not None:
            try:
                context_gdf.to_crs(crs).plot(
                    ax=ax,
                    color=self.ctx_fill,
                    edgecolor=self.ctx_edge,
                    linewidth=1.0,
                    zorder=1
                )
            except Exception:
                pass

        try:
            mainland_gdf.to_crs(crs).boundary.plot(
                ax=ax,
                color=self.country_border,
                linewidth=border_linewidth,
                zorder=5
            )
        except Exception:
            pass

        if admin_gdf is not None:
            try:
                admin_gdf.to_crs(crs).boundary.plot(
                    ax=ax,
                    color="#4A5568",
                    linewidth=1.2,
                    alpha=0.6,
                    linestyle="--",
                    zorder=4
                )
            except Exception:
                pass

    def draw_admin_labels(
        self,
        ax: plt.Axes,
        admin_gdf: gpd.GeoDataFrame,
        minx: float,
        maxx: float,
        miny: float,
        maxy: float,
        max_labels: int = 10,
        zorder: int = 7,
        fontsize: Optional[float] = None
    ) -> None:
        """
        Draw administrative region name labels on the map.

        Labels are placed at precomputed centroids and clipped to the
        data extent. Only the largest regions (by area) are labeled.

        Args:
            ax: Matplotlib axes.
            admin_gdf: GeoDataFrame with '_admin_name' and '_centroid' columns.
            minx: Minimum longitude of data extent.
            maxx: Maximum longitude of data extent.
            miny: Minimum latitude of data extent.
            maxy: Maximum latitude of data extent.
            max_labels: Maximum number of labels to draw.
            zorder: Drawing order.
            fontsize: Font size override (uses ADMIN_LABEL_FONTSIZE if None).
        """
        if admin_gdf is None:
            return

        fsz = fontsize if fontsize is not None else self.ADMIN_LABEL_FONTSIZE

        try:
            from src.utils.utils import get_local_utm_crs

            gdf_s = admin_gdf.copy()
            union = (
                gdf_s.geometry.union_all()
                if hasattr(gdf_s.geometry, "union_all")
                else gdf_s.geometry.unary_union
            )
            utm = get_local_utm_crs(union)
            gdf_s["_area"] = gdf_s.to_crs(utm).geometry.area
            gdf_s = (
                gdf_s.sort_values("_area", ascending=False)
                .head(max_labels)
            )

            for _, row in gdf_s.iterrows():
                name = str(row.get("_admin_name", "")).strip()
                if not name:
                    continue

                cx = row["_centroid"].x
                cy = row["_centroid"].y

                if not (minx <= cx <= maxx and miny <= cy <= maxy):
                    continue

                txt = ax.text(
                    cx,
                    cy,
                    name,
                    fontsize=fsz,
                    fontweight="bold",
                    ha="center",
                    va="center",
                    color="#1A1A1A",
                    zorder=zorder,
                    alpha=0.9
                )
                txt.set_path_effects([
                    patheffects.withStroke(
                        linewidth=2.5,
                        foreground="white",
                        alpha=0.85
                    )
                ])

        except Exception as exc:
            logger.debug("draw_admin_labels failed: %s", exc)

    def load_admin_boundaries(
        self,
        country_name: str,
        mainland_gdf: gpd.GeoDataFrame,
        raw_path: Path
    ) -> Optional[gpd.GeoDataFrame]:
        """
        Load and prepare administrative level-1 boundaries from disk.

        Searches for a matching shapefile under raw_path/countries_borders/,
        reprojects to EPSG:4326, filters to mainland extent, normalizes
        names to ASCII uppercase, and computes UTM centroids.

        Args:
            country_name: Country name for directory matching.
            mainland_gdf: GeoDataFrame of mainland geometry (for clipping).
            raw_path: Base path to raw data directory.

        Returns:
            Prepared GeoDataFrame with '_admin_name' and '_centroid' columns,
            or None if not found or on error.
        """
        try:
            from src.utils.utils import get_local_utm_crs

            borders_base = Path(raw_path) / "countries_borders"
            admin1_path = None

            for candidate_dir in borders_base.iterdir():
                if not candidate_dir.is_dir():
                    continue
                if country_name.lower() in candidate_dir.name.lower():
                    matches = list(candidate_dir.rglob("*_1.shp"))
                    if matches:
                        admin1_path = matches[0]
                        break

            if admin1_path is None:
                return None

            gdf = gpd.read_file(str(admin1_path))

            if str(gdf.crs) != "EPSG:4326":
                gdf = gdf.to_crs("EPSG:4326")

            union = (
                mainland_gdf.geometry.union_all()
                if hasattr(mainland_gdf.geometry, "union_all")
                else mainland_gdf.geometry.unary_union
            )
            gdf = gdf[gdf.geometry.intersects(union)].copy()

            if gdf.empty:
                return None

            name_col = next(
                (c for c in [
                    "NAME_1",
                    "name_1",
                    "NM_DISTRI",
                    "VARNAME_1",
                    "NAME"
                ] if c in gdf.columns),
                None
            )

            if name_col:
                gdf["_admin_name"] = (
                    gdf[name_col]
                    .astype(str)
                    .apply(
                        lambda x: (
                            unicodedata.normalize("NFKD", x)
                            .encode("ASCII", "ignore")
                            .decode("utf-8")
                            .upper()
                        )
                    )
                )
            else:
                gdf["_admin_name"] = ""

            utm = get_local_utm_crs(union)
            gdf["_centroid"] = (
                gdf.to_crs(utm).geometry.centroid.to_crs("EPSG:4326")
            )

            return gdf

        except Exception as exc:
            logger.debug("load_admin_boundaries failed: %s", exc)
            return None

    def add_standard_title(
        self,
        fig: plt.Figure,
        title_main: str,
        title_sub: Optional[str] = None,
        y_main: Optional[float] = None,
        y_sub: Optional[float] = None
    ) -> None:
        """
        Add a centered title (and optional subtitle) to the figure.

        Positions are computed relative to the reserved top margin.

        Args:
            fig: Matplotlib figure.
            title_main: Primary title text.
            title_sub: Optional subtitle text.
            y_main: Override y-position for main title (figure fraction).
            y_sub: Override y-position for subtitle (figure fraction).
        """
        top_margin_frac = self.TOP_IN / fig.get_figheight()
        ax_top = 1.0 - top_margin_frac

        if y_main is None:
            y_main = ax_top + (
                top_margin_frac * (0.60 if title_sub else 0.45)
            )

        fig.text(
            0.5,
            y_main,
            title_main,
            ha="center",
            va="center",
            fontsize=self.TITLE_FONTSIZE_MAIN,
            fontweight="bold",
            color="#1A1A1A",
            transform=fig.transFigure
        )

        if title_sub:
            gap = top_margin_frac * 0.35
            y_sub_calc = y_sub if y_sub is not None else y_main - gap
            fig.text(
                0.5,
                y_sub_calc,
                title_sub,
                ha="center",
                va="center",
                fontsize=self.TITLE_FONTSIZE_SUB,
                fontweight="bold",
                color="#444444",
                transform=fig.transFigure
            )

    def add_standard_footer(
        self,
        fig: plt.Figure,
        params_text: Optional[str] = None,
        stats_text: Optional[str] = None,
        crs_metadata: Optional[str] = None,
        y_params: Optional[float] = None,
        y_stats: Optional[float] = None,
        y_crs: Optional[float] = None
    ) -> None:
        """
        Add standard footer rows to the figure.

        Renders up to three footer lines: parameters text, statistics text,
        and CRS metadata. Positions are computed from the bottom margin.

        Args:
            fig: Matplotlib figure.
            params_text: Optional parameters summary string.
            stats_text: Optional statistics summary string.
            crs_metadata: Optional CRS/projection metadata string.
            y_params: Override y-position for params text.
            y_stats: Override y-position for stats text.
            y_crs: Override y-position for CRS text.
        """
        ax_cx = self.axes_center_x(fig)
        map_axes = [
            a for a in fig.axes
            if not getattr(a, "_is_cbar_ax", False)
        ]

        bottom_margin_frac = self.BOT_IN / fig.get_figheight()

        _y_crs = y_crs if y_crs is not None else 0.015
        _y_stats = (
            y_stats if y_stats is not None
            else bottom_margin_frac * 0.22
        )
        _y_params = (
            y_params if y_params is not None
            else bottom_margin_frac * 0.48
        )

        if params_text:
            fig.text(
                ax_cx,
                _y_params,
                params_text,
                ha="center",
                fontsize=self.FOOTER_FONTSIZE_PARAMS,
                color="#555555",
                transform=fig.transFigure,
                fontweight="medium"
            )

        if stats_text:
            fig.text(
                ax_cx,
                _y_stats,
                stats_text,
                ha="center",
                fontsize=self.FOOTER_FONTSIZE_STATS,
                color="#222222",
                transform=fig.transFigure
            )

        if not crs_metadata:
            if map_axes:
                ax = map_axes[0]
                ax_minx = ax.get_xlim()[0]
                ax_maxy = ax.get_ylim()[1]

                if abs(ax_minx) <= 180 and abs(ax_maxy) <= 90:
                    mid_lat = (ax.get_ylim()[0] + ax_maxy) / 2.0
                    crs_metadata = (
                        f"Data CRS: WGS 84 | "
                        f"Visual Aspect Corrected (lat {mid_lat:.1f}°)"
                    )
                else:
                    crs_metadata = "CRS: Projected (Metric)"
            else:
                crs_metadata = "CRS: WGS 84 (EPSG:4326)"

        fig.text(
            0.98,
            _y_crs,
            crs_metadata,
            ha="right",
            va="bottom",
            fontsize=11,
            color="#888888",
            style="italic",
            transform=fig.transFigure
        )

    def add_standard_legend(
        self,
        ax: plt.Axes,
        legend_elements: List,
        location: str = "lower_right",
        bbox_anchor: Optional[Tuple[float, float]] = None,
        ncol: int = 1
    ) -> Optional[plt.legend]:
        """
        Add a styled legend to the axes.

        Args:
            ax: Matplotlib axes.
            legend_elements: List of legend handles.
            location: Named location key (e.g., 'lower_right', 'upper_left').
            bbox_anchor: Override bbox_to_anchor coordinates.
            ncol: Number of legend columns.

        Returns:
            Matplotlib Legend object or None.
        """
        loc_map = {
            "lower_center": ("upper center", (0.5, -0.03)),
            "lower_right": ("lower right", (0.98, 0.03)),
            "lower_left": ("lower left", (0.02, 0.03)),
            "upper_right": ("upper right", (0.98, 0.97)),
            "upper_left": ("upper left", (0.02, 0.97)),
        }

        loc, default_bbox = loc_map.get(
            location,
            (location.replace("_", " "), None)
        )
        bbox = bbox_anchor if bbox_anchor else default_bbox

        legend = ax.legend(
            handles=legend_elements,
            loc=loc,
            bbox_to_anchor=bbox,
            fontsize=self.LEGEND_FONTSIZE,
            framealpha=0.95,
            edgecolor="#AAAAAA",
            fancybox=True,
            ncol=ncol,
            handlelength=2.0,
            borderpad=0.5,
            handletextpad=0.5,
            labelspacing=0.5,
        )
        legend.get_frame().set_linewidth(1.0)
        return legend

    def add_stats_strip(
        self,
        fig: plt.Figure,
        stats: Dict[str, float],
        format_template: str = (
            "Mean: {mean:.1f} ± {std:.1f}  |  "
            "P10: {p10:.1f}  |  "
            "IQR: {p25:.1f}–{p75:.1f}  |  "
            "P90: {p90:.1f}"
        ),
        y_position: float = 0.025,
        unit: str = ""
    ) -> None:
        """
        Render a statistics summary strip in the figure footer.

        Uses safe dict formatting to substitute available stats keys.
        Missing keys are replaced with 'N/A'.

        Args:
            fig: Matplotlib figure.
            stats: Dictionary of statistic values (e.g., mean, std, p10).
            format_template: Format string with named placeholders.
            y_position: y-position in figure fraction.
            unit: Optional unit string appended to the text.
        """
        ax_cx = self.axes_center_x(fig)

        class SafeDict(dict):
            def __missing__(self, key):
                return "N/A"

        text = format_template.format_map(SafeDict(stats)) + unit
        fig.text(
            ax_cx,
            y_position,
            text,
            ha="center",
            fontsize=self.FOOTER_FONTSIZE_STATS,
            color="#222222",
            transform=fig.transFigure
        )

    def save_to_buffer(
        self,
        fig: plt.Figure,
        format: str = "png",
        pad_inches: float = 0.1
    ) -> io.BytesIO:
        """
        Save figure to an in-memory buffer.

        Args:
            fig: Matplotlib figure to save.
            format: Image format string (e.g., 'png', 'pdf').
            pad_inches: Padding around the figure.

        Returns:
            BytesIO buffer containing the rendered figure.
        """
        buf = io.BytesIO()
        fig.savefig(
            buf,
            bbox_inches="tight",
            pad_inches=pad_inches,
            dpi=self.dpi,
            facecolor=fig.get_facecolor(),
            format=format
        )
        fig.clf()
        plt.close(fig)
        buf.seek(0)
        return buf

    # ══════════════════════════════════════════════
    # COMPASS ROSE
    # ══════════════════════════════════════════════

    def _draw_compass_rose(
        self,
        ax: plt.Axes,
        cx: float,
        cy: float,
        arrow_length: float,
        zorder: int = 10
    ) -> None:
        """
        Draw a compass rose (north arrow) on the axes.

        Args:
            ax: Matplotlib axes.
            cx: x-center of the compass rose (data coordinates).
            cy: Base y-position of the compass rose (data coordinates).
            arrow_length: Length of the north arrow in data coordinates.
            zorder: Drawing order.
        """
        hw = arrow_length * 0.18
        notch_y = cy + arrow_length * 0.22
        tip_y = cy + arrow_length
        stroke = [
            patheffects.withStroke(
                linewidth=3.5,
                foreground="white",
                alpha=0.9
            )
        ]

        ax.add_patch(
            plt.Polygon(
                [(cx, tip_y), (cx - hw, cy), (cx, notch_y)],
                closed=True,
                fc="#1A1A1A",
                ec="#1A1A1A",
                lw=0.8,
                zorder=zorder
            )
        )
        ax.add_patch(
            plt.Polygon(
                [(cx, tip_y), (cx + hw, cy), (cx, notch_y)],
                closed=True,
                fc="#FFFFFF",
                ec="#1A1A1A",
                lw=0.8,
                zorder=zorder
            )
        )

        tick = arrow_length * 0.14
        ax.plot(
            [cx - tick, cx + tick],
            [notch_y, notch_y],
            color="#1A1A1A",
            lw=1.2,
            zorder=zorder,
            solid_capstyle="butt"
        )
        ax.text(
            cx,
            tip_y + arrow_length * 0.08,
            "N",
            fontsize=13,
            fontweight="bold",
            ha="center",
            va="bottom",
            color="#1A1A1A",
            zorder=zorder + 1,
            path_effects=stroke
        )

    # ══════════════════════════════════════════════
    # SEGMENTED SCALE BAR
    # ══════════════════════════════════════════════

    def _draw_segmented_scalebar(
        self,
        ax: plt.Axes,
        x0: float,
        y0: float,
        bar_km: int,
        km_per_deg_lon: float,
        n_segments: Optional[int] = None,
        zorder: int = 10,
        draw_background: bool = False
    ) -> None:
        """
        Draw a segmented scale bar on the axes.

        Args:
            ax: Matplotlib axes.
            x0: Left x-position of the scale bar (data coordinates).
            y0: Bottom y-position of the scale bar (data coordinates).
            bar_km: Total scale bar length in kilometers.
            km_per_deg_lon: Kilometers per degree longitude at the map center.
            n_segments: Number of alternating segments (auto-detected if None).
            zorder: Drawing order.
            draw_background: If True, draw a background rectangle.
        """
        if n_segments is None:
            for n in (4, 5, 2, 3):
                if bar_km % n == 0:
                    n_segments = n
                    break
            else:
                n_segments = 4

        stroke = [
            patheffects.withStroke(
                linewidth=2.5,
                foreground="white",
                alpha=0.85
            )
        ]
        eff_lat = ax.get_ylim()[1] - ax.get_ylim()[0]
        bar_h = eff_lat * 0.006
        total_deg = bar_km / km_per_deg_lon
        seg_deg = total_deg / n_segments
        seg_km = bar_km / n_segments

        if draw_background:
            bg_color = ax.get_facecolor()
            pad = eff_lat * 0.006
            bg_x = x0 - pad
            bg_y = y0 - eff_lat * 0.015 - pad
            bg_w = total_deg + pad * 2 + eff_lat * 0.025
            bg_h = bar_h + eff_lat * 0.015 + pad * 2 + eff_lat * 0.005

            ax.add_patch(
                plt.Rectangle(
                    (bg_x, bg_y),
                    bg_w,
                    bg_h,
                    fc=bg_color,
                    ec="none",
                    alpha=0.88,
                    zorder=zorder - 1
                )
            )

        for i in range(n_segments):
            fc = "#1A1A1A" if i % 2 == 0 else "#FFFFFF"
            ax.add_patch(
                plt.Rectangle(
                    (x0 + i * seg_deg, y0),
                    seg_deg,
                    bar_h,
                    fc=fc,
                    ec="#1A1A1A",
                    lw=0.9,
                    zorder=zorder
                )
            )

        label_y = y0 - eff_lat * 0.010

        for i in range(n_segments + 1):
            ax.text(
                x0 + i * seg_deg,
                label_y,
                str(int(round(i * seg_km))),
                fontsize=9,
                ha="center",
                va="top",
                fontweight="bold",
                color="#1A1A1A",
                zorder=zorder + 1,
                path_effects=stroke
            )

        ax.text(
            x0 + total_deg + eff_lat * 0.008,
            y0 + bar_h * 0.5,
            "km",
            fontsize=9,
            ha="left",
            va="center",
            fontweight="bold",
            color="#1A1A1A",
            zorder=zorder + 1,
            path_effects=stroke
        )

    # ══════════════════════════════════════════════
    # DECORATIONS
    # ══════════════════════════════════════════════

    def add_decorations(
        self,
        ax: plt.Axes,
        minx: float,
        maxx: float,
        miny: float,
        maxy: float,
        fixed_scalebar_km: Optional[int] = None,
        **kwargs
    ) -> None:
        """
        Add standard map decorations: aspect correction, compass rose, scale bar, axes labels.

        Args:
            ax: Matplotlib axes.
            minx: Minimum longitude of data.
            maxx: Maximum longitude of data.
            miny: Minimum latitude of data.
            maxy: Maximum latitude of data.
            fixed_scalebar_km: Override scale bar length (km). Auto-computed if None.
            **kwargs: Unused extra keyword arguments.
        """
        is_geographic = abs(minx) <= 180 and abs(maxy) <= 90
        mid_lat = (miny + maxy) / 2.0
        cos_mid_lat = (
            math.cos(math.radians(mid_lat))
            if is_geographic
            else 1.0
        )

        ax.set_aspect(
            1.0 / cos_mid_lat if is_geographic else 1.0,
            adjustable="box"
        )

        ax_minx, ax_maxx = ax.get_xlim()
        ax_miny, ax_maxy = ax.get_ylim()
        eff_lon = ax_maxx - ax_minx
        eff_lat = ax_maxy - ax_miny

        km_per_deg_lon = (math.pi / 180.0) * 6371.0 * cos_mid_lat
        map_width_km = (maxx - minx) * km_per_deg_lon

        if fixed_scalebar_km is not None:
            bar_km = fixed_scalebar_km
        else:
            raw = map_width_km / 5.0
            if raw > 100:
                bar_km = round(raw / 50) * 50
            elif raw > 20:
                bar_km = round(raw / 10) * 10
            else:
                bar_km = 10 if raw > 5 else 5

        rose_cx = ax_minx + eff_lon * 0.05
        arrow_len = eff_lat * 0.04
        rose_base_y = ax_maxy - (eff_lat * 0.10)
        self._draw_compass_rose(ax, rose_cx, rose_base_y, arrow_len)

        bar_y = ax_miny + ((miny - ax_miny) * 0.35)
        bar_x0 = ax_minx + eff_lon * 0.05
        self._draw_segmented_scalebar(
            ax,
            bar_x0,
            bar_y,
            bar_km,
            km_per_deg_lon,
            draw_background=True
        )

        ax.set_xlabel(
            "Longitude",
            fontsize=self.AXES_LABEL_FONTSIZE,
            labelpad=8,
            color="#333333"
        )
        ax.set_ylabel(
            "Latitude",
            fontsize=self.AXES_LABEL_FONTSIZE,
            labelpad=8,
            color="#333333"
        )
        ax.tick_params(
            labelsize=self.AXES_TICK_FONTSIZE,
            colors="#333333",
            width=1.2,
            length=4
        )
        ax.grid(False)

    # ══════════════════════════════════════════════
    # COLORBAR
    # ══════════════════════════════════════════════

    def add_colorbar(
        self,
        fig: plt.Figure,
        im,
        unit_label: str,
        extend: str = "neither"
    ):
        """
        Add a vertical colorbar to the right of the map axes.

        Args:
            fig: Matplotlib figure.
            im: Mappable object (e.g., from ax.imshow or ax.scatter).
            unit_label: Label text for the colorbar.
            extend: Colorbar extension ('neither', 'min', 'max', 'both').

        Returns:
            Matplotlib Colorbar object.
        """
        ax = im.axes
        pos = ax.get_position()

        cax_h = pos.height * 0.68
        cax_y = pos.y0 + (pos.height - cax_h) / 2
        cax_w = 0.038

        cax = fig.add_axes([pos.x1 + 0.028, cax_y, cax_w, cax_h])
        cax._is_cbar_ax = True

        cbar = fig.colorbar(im, cax=cax, orientation="vertical", extend=extend)
        cbar.set_label(
            unit_label,
            fontsize=14,
            labelpad=12,
            fontweight="bold"
        )
        cbar.ax.tick_params(labelsize=12.5)
        return cbar

    # ══════════════════════════════════════════════
    # TRIPLE COMPARISON VIA PIL
    # ══════════════════════════════════════════════

    def create_comparison_via_pil(
        self,
        individual_plot_func,
        plot_data_list: List[Dict],
        country_name: str,
        title_line1: str,
        title_line2: str,
        out_path: Path,
        gap_px: int = 28
    ) -> None:
        """
        Compose multiple plot panels into a single image using PIL.

        Each panel is rendered individually via individual_plot_func,
        captured to a buffer, and composited side-by-side with a
        shared title header strip.

        Args:
            individual_plot_func: Callable that renders a single panel.
            plot_data_list: List of keyword argument dicts for each panel.
            country_name: Country name (available in plot_data_list items).
            title_line1: Primary title for the composite image.
            title_line2: Secondary title for the composite image.
            out_path: Output file path for the composite image.
            gap_px: Pixel gap between panels.
        """
        if _PIL_Image is None:
            logger.error("PIL not available — cannot create comparison image.")
            return

        panel_images = []
        original_save = self.save

        for data in plot_data_list:
            buf = io.BytesIO()

            def _save_to_buf(
                fig: plt.Figure,
                _path: Path,
                _buf: io.BytesIO = buf
            ) -> None:
                fig.savefig(
                    _buf,
                    bbox_inches="tight",
                    pad_inches=0.10,
                    dpi=self.dpi,
                    facecolor=fig.get_facecolor(),
                    format="png"
                )
                fig.clf()
                plt.close(fig)

            self.save = _save_to_buf

            try:
                individual_plot_func(**data)
            except Exception as exc:
                logger.warning("Panel render error: %s", exc)
                continue
            finally:
                self.save = original_save

            buf.seek(0)
            img = _PIL_Image.open(buf).convert("RGB")
            panel_images.append(img.copy())
            buf.close()

        if not panel_images:
            return

        bg_rgb = panel_images[0].getpixel((5, 5))
        bg_hex = "#{:02x}{:02x}{:02x}".format(*bg_rgb)
        target_h = max(img.height for img in panel_images)

        def _pad_h(img):
            if img.height == target_h:
                return img
            p = _PIL_Image.new("RGB", (img.width, target_h), bg_rgb)
            p.paste(img, (0, (target_h - img.height) // 2))
            return p

        panel_images = [_pad_h(i) for i in panel_images]

        total_w = (
            sum(i.width for i in panel_images)
            + gap_px * (len(panel_images) - 1)
        )

        HEADER_PX = max(110, int(total_w * 0.040))
        FOOTER_PX = max(8, int(total_w * 0.003))
        canvas_h = target_h + HEADER_PX + FOOTER_PX

        canvas = _PIL_Image.new("RGB", (total_w, canvas_h), bg_rgb)
        x_cur = 0

        for img in panel_images:
            canvas.paste(img, (x_cur, HEADER_PX))
            x_cur += img.width + gap_px

        fig_w_in = canvas.width / self.dpi
        fig_h_in = canvas.height / self.dpi

        tfig = plt.figure(figsize=(fig_w_in, fig_h_in), dpi=self.dpi)
        tfig.patch.set_facecolor(bg_hex)
        axi = tfig.add_axes([0, 0, 1, 1])
        axi.axis("off")
        axi.imshow(np.array(canvas), aspect="auto")

        header_mid = 1.0 - (HEADER_PX * 0.50) / canvas_h
        half_gap = (HEADER_PX * 0.26) / canvas_h

        fs_main = max(16, min(int(fig_w_in * 0.90), 38))
        fs_sub = max(12, min(int(fig_w_in * 0.62), 28))

        tfig.text(
            0.5,
            header_mid + half_gap,
            title_line1,
            ha="center",
            va="center",
            fontsize=fs_main,
            fontweight="bold",
            color="#1A1A1A",
            transform=tfig.transFigure
        )
        tfig.text(
            0.5,
            header_mid - half_gap,
            title_line2,
            ha="center",
            va="center",
            fontsize=fs_sub,
            fontweight="bold",
            color="#333333",
            transform=tfig.transFigure
        )

        out_path.parent.mkdir(parents=True, exist_ok=True)
        tfig.savefig(
            str(out_path),
            dpi=self.dpi,
            bbox_inches="tight",
            pad_inches=0.12,
            facecolor=bg_hex
        )
        plt.close(tfig)

    def save(self, fig: plt.Figure, out_path: Path) -> None:
        """
        Save figure to disk and close it.

        Args:
            fig: Matplotlib figure to save.
            out_path: Destination file path.
        """
        out_path.parent.mkdir(parents=True, exist_ok=True)
        fig.savefig(
            str(out_path),
            bbox_inches="tight",
            pad_inches=0.10,
            dpi=self.dpi,
            facecolor=fig.get_facecolor()
        )
        logger.debug("Map saved: %s", out_path)
        fig.clf()
        plt.close(fig)