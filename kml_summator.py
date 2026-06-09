#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import re
import zipfile
from collections import defaultdict
from datetime import datetime
from pathlib import Path

import simplekml
from lxml import etree
from pyproj import Geod
from shapely.geometry import MultiPolygon, Polygon
from shapely.ops import unary_union

# =====================================================
# CONFIG
# =====================================================

INPUT_DIR = Path(r"D:\work\python\Meteorology_works\Hysplit_model_dispersion\result_kml")
OUTPUT_FILE = Path(r"result.kml")

NS = {
    "kml": "http://www.opengis.net/kml/2.2",
}

LEVEL_RE = re.compile(
    r"Contour Level:\s*([0-9Ee+\-\.]+)",
    re.IGNORECASE,
)

DATE_RE = re.compile(
    r"(\d{4})(\d{2})(\d{2})(\d{2})",
)

STYLE_MAP = {
    1e-5: "C8FFFFFF",
    1e-4: "C800FFFF",
    1e-3: "C8FF0000",
    1e-2: "C800FF00",
    1e-1: "C80000FF",
}

geod = Geod(ellps="WGS84")

# =====================================================
# HELPERS
# =====================================================


def open_kml(path):
    if path.suffix.lower() == ".kml":
        return etree.parse(str(path))

    if path.suffix.lower() == ".kmz":
        with zipfile.ZipFile(path) as z:
            for name in z.namelist():
                if name.lower().endswith(".kml"):
                    with z.open(name) as f:
                        return etree.parse(f)

    return None


def parse_level(text):
    if not text:
        return None

    m = LEVEL_RE.search(text)

    if not m:
        return None

    return float(m.group(1))


def parse_datetime(filename):
    m = DATE_RE.search(filename)

    if not m:
        return None

    y, mo, d, h = map(int, m.groups())

    return datetime(y, mo, d, h)


def parse_coordinates(text):
    coords = []

    for row in text.strip().split():
        vals = row.split(",")

        if len(vals) < 2:
            continue

        coords.append(
            (
                float(vals[0]),
                float(vals[1]),
            ),
        )

    return coords


def area_km2(poly):
    lon, lat = poly.exterior.xy

    area, _ = geod.polygon_area_perimeter(
        lon,
        lat,
    )

    return abs(area) / 1e6


def color_for_level(level):
    result = "C8FFFFFF"

    for limit in sorted(STYLE_MAP):
        if level >= limit:
            result = STYLE_MAP[limit]

    return result


# =====================================================
# READING
# =====================================================


def read_file(path):
    tree = open_kml(path)

    if tree is None:
        return {}, []

    contours = defaultdict(list)
    max_cells = []

    placemarks = tree.findall(
        ".//kml:Placemark",
        NS,
    )

    for pm in placemarks:
        name = pm.findtext(
            "kml:name",
            default="",
            namespaces=NS,
        )

        description = pm.findtext(
            "kml:description",
            default="",
            namespaces=NS,
        )

        text = f"{name}\n{description}"

        if "Maximum Value Grid Cell" in text:
            polygons = pm.findall(
                ".//kml:Polygon",
                NS,
            )

            for poly in polygons:
                coords_node = poly.find(
                    ".//kml:coordinates",
                    NS,
                )

                if coords_node is None:
                    continue

                coords = parse_coordinates(
                    coords_node.text,
                )

                if len(coords) >= 3:
                    try:
                        max_cells.append(
                            Polygon(coords),
                        )
                    except Exception:
                        pass

            continue

        level = parse_level(text)

        if level is None:
            continue

        polygons = pm.findall(
            ".//kml:Polygon",
            NS,
        )

        for poly in polygons:
            coords_node = poly.find(
                ".//kml:coordinates",
                NS,
            )

            if coords_node is None:
                continue

            coords = parse_coordinates(
                coords_node.text,
            )

            if len(coords) < 3:
                continue

            try:
                geom = Polygon(coords)

                if not geom.is_valid:
                    geom = geom.buffer(0)

                if geom.is_valid:
                    contours[level].append(
                        geom,
                    )

            except Exception:
                pass

    return contours, max_cells


# =====================================================
# MAIN
# =====================================================


def main():
    files = sorted(INPUT_DIR.glob("*.kml")) + sorted(INPUT_DIR.glob("*.kmz"))

    if not files:
        print("No files found")
        return

    all_levels = defaultdict(list)
    all_max_cells = []

    dates = []

    for file in files:
        print(
            f"Processing {file.name}",
        )

        dt = parse_datetime(
            file.name,
        )

        if dt:
            dates.append(dt)

        contours, max_cells = read_file(
            file,
        )

        for level, polys in contours.items():
            all_levels[level].extend(
                polys,
            )

        all_max_cells.extend(
            max_cells,
        )

    kml = simplekml.Kml()

    summary_folder = kml.newfolder(
        name="Summary",
    )

    contours_folder = kml.newfolder(
        name="Contours",
    )

    summary = summary_folder.newpoint(
        name="Statistics",
    )

    summary.coords = [(0, 0)]

    text = []

    text.append(
        f"Files: {len(files)}",
    )

    if dates:
        text.append(
            f"Start: {min(dates)}",
        )

        text.append(
            f"End: {max(dates)}",
        )

    text.append("")
    text.append("Sources:")

    for file in files:
        text.append(file.name)

    summary.description = "\n".join(text)

    # -----------------------------------------
    # Contours
    # -----------------------------------------

    for level in sorted(
        all_levels.keys(),
    ):
        merged = unary_union(
            all_levels[level],
        )

        folder = contours_folder.newfolder(
            name=f"{level:.3E}",
        )

        geoms = []

        if isinstance(
            merged,
            Polygon,
        ):
            geoms = [merged]

        elif isinstance(
            merged,
            MultiPolygon,
        ):
            geoms = list(
                merged.geoms,
            )

        total_area = sum(area_km2(g) for g in geoms)

        for geom in geoms:
            poly = folder.newpolygon(
                name=f"{level:.3E}",
            )

            poly.outerboundaryis = list(
                geom.exterior.coords,
            )

            poly.description = f"Concentration: {level:.3E}\nArea: {total_area:.2f} km²"

            poly.style.polystyle.color = color_for_level(level)

            poly.style.linestyle.width = 1

    # -----------------------------------------
    # Maximum cells
    # -----------------------------------------

    if all_max_cells:
        max_folder = kml.newfolder(
            name="Maximum Value Grid Cell",
        )

        for geom in all_max_cells:
            poly = max_folder.newpolygon(
                name="Maximum",
            )

            poly.outerboundaryis = list(
                geom.exterior.coords,
            )

            poly.style.polystyle.color = "AA0000FF"

    kml.save(str(OUTPUT_FILE))

    print(
        f"Saved: {OUTPUT_FILE}",
    )


if __name__ == "__main__":
    main()
