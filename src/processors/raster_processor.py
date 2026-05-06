"""
raster_processor.py
===================
Utilitários de processamento raster para o GeoWorld Framework.

Fase de Pré-processamento — calcula declividade (Slope) a partir de DEMs.
Leitura por blocos de 512 linhas para controle de memória em DEMs globais.
"""

from __future__ import annotations

import logging
from pathlib import Path

import numpy as np
import rasterio
from rasterio.windows import Window

from src.core.constants import KM_PER_DEG_LAT, NODATA_FLOAT
from src.utils.utils import safe_raster_open, safe_raster_write


class RasterProcessor:
    def __init__(self):
        self.logger = logging.getLogger("geoworld.processors.RasterProcessor")

    def calculate_slope(self, dem_path: Path, output_path: Path) -> None:
        """
        Calcula declividade em graus a partir de um DEM via gradiente NumPy.
        Processa por blocos de 512 linhas para memória controlada.

        Correção geométrica por latitude: `dx` varia com `cos(lat)`, garantindo
        que a inclinação seja precisa em coordenadas geográficas (EPSG:4326).

        Nota: gc.collect() removido (7.2.4) — arrays liberados automaticamente
        pelo reference counting do CPython ao sair do escopo da iteração.
        """
        self.logger.info(f"Calculando Slope: {dem_path.name}")

        with safe_raster_open(dem_path) as src:
            profile    = src.profile.copy()
            res_x, res_y = src.res
            nodata_val = src.nodata if src.nodata is not None else NODATA_FLOAT
            km_per_deg = KM_PER_DEG_LAT
            dy         = res_y * km_per_deg * 1000  # metros por pixel em Y

            profile.update(
                dtype      = rasterio.float32,
                nodata     = nodata_val,
                compress   = "lzw",
                tiled      = True,
                blockxsize = 256,
                blockysize = 256,
            )

            with safe_raster_write(output_path, **profile) as dst:
                block_height = 512

                for y in range(0, src.height, block_height):
                    h = min(block_height, src.height - y)

                    # Lê bloco com padding de ±1 linha para gradiente correto nas bordas
                    win_y_start = max(0, y - 1)
                    win_y_end   = min(src.height, y + h + 1)
                    win_h       = win_y_end - win_y_start

                    elev_block = src.read(
                        1, window=Window(0, win_y_start, src.width, win_h)
                    ).astype(np.float32)

                    # dx corrigido por latitude (distância horizontal ∝ cos(lat))
                    rows_idx = np.arange(win_y_start, win_y_end)
                    _, y_coords = src.xy(rows_idx, np.zeros(len(rows_idx)))
                    lat_grid    = np.array(y_coords).reshape(-1, 1)
                    dx_block    = res_x * km_per_deg * 1000 * np.cos(np.radians(lat_grid))

                    # Gradiente central NumPy
                    dz_drow, dz_dcol = np.gradient(elev_block, 1, 1)
                    dz_dx = dz_dcol / dx_block
                    dz_dy = dz_drow / dy

                    slope_deg = np.degrees(np.arctan(np.sqrt(dz_dx**2 + dz_dy**2)))
                    slope_deg[elev_block == nodata_val] = nodata_val

                    # Descarta padding antes de escrever
                    offset_top  = 1 if y > 0 else 0
                    final_block = slope_deg[offset_top: offset_top + h, :]

                    dst.write(
                        final_block.astype(np.float32), 1,
                        window=Window(0, y, src.width, h),
                    )
                    # Arrays liberados automaticamente — sem gc.collect() necessário

        self.logger.info(f"Slope concluído: {output_path.name}")