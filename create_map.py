from pathlib import Path

import contextily as ctx
import geopandas as gpd
import matplotlib.pyplot as plt


def kml_to_png(
    kml_file: str,
    output_png: str = "result.png",
    dpi: int = 300,
):
    """
    Визуализация KML на географической карте
    с сохранением результата в PNG.
    """

    kml_file = Path(kml_file)

    # Включаем поддержку KML
    gpd.io.file.fiona.drvsupport.supported_drivers["KML"] = "rw"

    gdf = gpd.read_file(kml_file, driver="KML")

    if gdf.empty:
        raise ValueError("KML не содержит объектов")

    # Переход в Web Mercator
    gdf = gdf.to_crs(epsg=3857)

    fig, ax = plt.subplots(
        figsize=(12, 12),
    )

    # Полигоны
    polygons = gdf[
        gdf.geometry.geom_type.isin(
            ["Polygon", "MultiPolygon"],
        )
    ]

    if not polygons.empty:
        polygons.plot(
            ax=ax,
            alpha=0.5,
            edgecolor="black",
            linewidth=0.5,
        )

    # Линии
    lines = gdf[
        gdf.geometry.geom_type.isin(
            ["LineString", "MultiLineString"],
        )
    ]

    if not lines.empty:
        lines.plot(
            ax=ax,
            linewidth=2,
        )

    # Точки
    points = gdf[
        gdf.geometry.geom_type.isin(
            ["Point", "MultiPoint"],
        )
    ]

    if not points.empty:
        points.plot(
            ax=ax,
            markersize=20,
        )

    # Географическая подложка
    ctx.add_basemap(
        ax,
        source=ctx.providers.OpenStreetMap.Mapnik,
    )

    ax.set_axis_off()

    plt.tight_layout()

    plt.savefig(
        output_png,
        dpi=dpi,
        bbox_inches="tight",
    )

    plt.close()

    print(f"Сохранено: {output_png}")


if __name__ == "__main__":
    kml_to_png(
        "result.kml",
        "result.png",
    )
