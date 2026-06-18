"""
average_concentration_kml_batched_optimized.py

Формирует усреднённое поле концентраций по нескольким KML-файлам HYSPLIT.

Что делает:
1. Читает все *.kml из INPUT_DIR.
2. Обрабатывает файлы батчами, чтобы не держать все исходные слои в одном overlay.
3. Сохраняет уникальные реальные источники выброса.
4. Ускоряет расчёт:
   - предварительно упрощает геометрию;
   - опционально заполняет внутренние дырки полигонов;
   - использует STRtree для поиска пересечений;
   - отбрасывает очень мелкие области;
   - ограничивает число вершин перед записью итогового KML.
5. Считает среднее поле: сумма концентраций / количество KML-файлов.
6. Группирует близкие по порядку значения концентрации.
7. Записывает один итоговый KML.

Промежуточные файлы НЕ сохраняются.

Зависимости:
    pip install shapely lxml
"""

import copy
import math
import re
import time
from pathlib import Path

from lxml import etree
from shapely.geometry import MultiPolygon, Polygon
from shapely.ops import polygonize, unary_union
from shapely.strtree import STRtree

# =============================================================================
# НАСТРОЙКИ
# =============================================================================

INPUT_DIR = Path(r"D:\work\python\Meteorology_works\Hysplit_model_dispersion\result_kml\data\Angarsk")
OUTPUT_FILE = Path("AVERAGE_CONCENTRATION_BATCHED_FAST_TIMED.kml")

# Сколько KML-файлов обрабатывается за один проход.
BATCH_SIZE = 5

# Предварительное упрощение геометрии при чтении.
# Увеличение ускоряет обработку, но снижает точность границ.
# Для координат в градусах 0.0001 ≈ 10 м, 0.0005 ≈ 50 м.
PRE_SIMPLIFY_TOL = 0.001


# Группировать близкие уровни концентрации ДО overlay.
# Это сильно уменьшает число контуров и элементарных областей,
# но является аппроксимацией: близкие значения заменяются средним уровнем группы.
PRE_GROUP_CLOSE_LEVELS_BEFORE_OVERLAY = True

# Заполнять внутренние пустоты/дырки внутри полигонов.
# Обычно ускоряет расчёт и уменьшает размер результата.
FILL_POLYGON_HOLES = True

# Удалять слишком маленькие элементарные области после polygonize.
# В координатах lon/lat это площадь в градусах^2. 0 = не фильтровать.
MIN_CELL_AREA = 0.0

# Округление концентраций перед объединением одинаковых уровней.
VALUE_ROUND_DIGITS = 12

# Сохранение уникальных реальных источников.
SAVE_SOURCES = True
DEDUP_SOURCES_BY_COORDS = True
SOURCE_COORD_ROUND = 6

# Группировка близких уровней концентрации по логарифмическому порядку.
MERGE_CLOSE_LEVELS = True
LOG10_GROUP_STEP = 0.25

# Значение, которое будет записано для группы:
#   area_weighted — среднее по площади;
#   arithmetic    — простое среднее уровней внутри группы;
#   max           — максимум внутри группы.
GROUP_VALUE_METHOD = "area_weighted"

# Ограничение количества вершин перед записью KML.
# 0 = не ограничивать.
MAX_KML_VERTICES = 120_000
WRITE_SIMPLIFY_START_TOL = 0.0005
WRITE_SIMPLIFY_GROWTH = 1.7
WRITE_SIMPLIFY_MAX_ITERATIONS = 10

# Прозрачность заливки в KML: ff = полностью непрозрачно, 80 = полупрозрачно.
KML_ALPHA = "c8"


# =============================================================================
# KML / GEOMETRY
# =============================================================================

NS = {"kml": "http://www.opengis.net/kml/2.2"}
PAT_NUMBER = re.compile(r"[-+]?\d*\.?\d+(?:[Ee][-+]?\d+)?")


class Contour:
    def __init__(self, geom, value):
        self.geom = geom
        self.value = value


def format_seconds(seconds):
    if seconds < 60:
        return f"{seconds:.2f} сек"

    minutes = int(seconds // 60)
    rest = seconds - minutes * 60
    return f"{minutes} мин {rest:.1f} сек"


def chunked(items, size):
    for i in range(0, len(items), size):
        yield items[i : i + size]


def parse_coords(text):
    points = []

    if not text:
        return points

    for item in text.split():
        parts = item.split(",")

        if len(parts) < 2:
            continue

        try:
            points.append((float(parts[0]), float(parts[1])))
        except ValueError:
            pass

    return points


def clean_geom(geom):
    if geom is None or geom.is_empty:
        return None

    if not geom.is_valid:
        geom = geom.buffer(0)

    if geom.is_empty:
        return None

    return geom


def remove_holes(geom):
    """Возвращает геометрию без внутренних дырок."""
    if geom is None or geom.is_empty:
        return geom

    if geom.geom_type == "Polygon":
        return Polygon(geom.exterior)

    if geom.geom_type == "MultiPolygon":
        polygons = [Polygon(poly.exterior) for poly in geom.geoms if not poly.is_empty]
        return MultiPolygon(polygons) if polygons else geom

    return geom


def make_polygon_from_kml_polygon(poly_node):
    outer_node = poly_node.find(
        ".//{%s}outerBoundaryIs/{%s}LinearRing/{%s}coordinates" % (NS["kml"], NS["kml"], NS["kml"]),
    )

    if outer_node is None or outer_node.text is None:
        return None

    exterior = parse_coords(outer_node.text)

    if len(exterior) < 3:
        return None

    holes = []

    if not FILL_POLYGON_HOLES:
        inner_nodes = poly_node.findall(
            ".//{%s}innerBoundaryIs/{%s}LinearRing/{%s}coordinates" % (NS["kml"], NS["kml"], NS["kml"]),
        )

        for inner_node in inner_nodes:
            ring = parse_coords(inner_node.text)

            if len(ring) >= 3:
                holes.append(ring)

    geom = Polygon(exterior, holes)
    geom = clean_geom(geom)

    if geom is None:
        return None

    if FILL_POLYGON_HOLES:
        geom = remove_holes(geom)
        geom = clean_geom(geom)

    if geom is None:
        return None

    if PRE_SIMPLIFY_TOL > 0:
        geom = geom.simplify(PRE_SIMPLIFY_TOL, preserve_topology=True)
        geom = clean_geom(geom)

    return geom


def is_source_placemark(placemark):
    """
    Определяет только реальные источники выброса HYSPLIT.
    Не сохраняет Contour Level и Maximum Value Grid Cell.
    """

    name_node = placemark.find("{%s}name" % NS["kml"])
    name_text = (name_node.text or "").lower() if name_node is not None else ""

    if "contour level" in name_text:
        return False

    if "maximum value grid cell" in name_text or "max value" in name_text:
        return False

    return placemark.find(".//{%s}Point" % NS["kml"]) is not None


def get_point_coord_from_placemark(placemark):
    coord_node = placemark.find(".//{%s}Point/{%s}coordinates" % (NS["kml"], NS["kml"]))

    if coord_node is None or coord_node.text is None:
        return None

    points = parse_coords(coord_node.text)

    if not points:
        return None

    lon, lat = points[0]
    return (round(lon, SOURCE_COORD_ROUND), round(lat, SOURCE_COORD_ROUND))


def source_key(placemark):
    if DEDUP_SOURCES_BY_COORDS:
        coord_key = get_point_coord_from_placemark(placemark)

        if coord_key is not None:
            return ("coord", coord_key)

    return ("xml", etree.tostring(placemark, encoding="utf-8"))


def parse_styles(tree):
    styles = {}

    for node in tree.xpath("//kml:Style | //kml:StyleMap", namespaces=NS):
        style_id = node.get("id")

        if style_id and style_id not in styles:
            styles[style_id] = copy.deepcopy(node)

    return styles


def group_contours_before_overlay(contours):
    """
    Предварительно объединяет близкие по порядку контуры внутри одного KML.
    Это уменьшает число геометрий до overlay и ускоряет polygonize/unary_union.
    """
    if not PRE_GROUP_CLOSE_LEVELS_BEFORE_OVERLAY or not MERGE_CLOSE_LEVELS:
        return contours

    groups = {}

    for contour in contours:
        key = concentration_group_key(contour.value)

        if key is None:
            continue

        groups.setdefault(key, []).append((contour.geom, contour.value))

    grouped = []

    for _key, items in groups.items():
        geoms = [geom for geom, _value in items if geom is not None and not geom.is_empty]

        if not geoms:
            continue

        merged = merge_geometries(geoms)

        if merged is None or merged.is_empty:
            continue

        group_value = calculate_group_value(items)

        if merged.geom_type == "Polygon":
            grouped.append(Contour(merged, round(group_value, VALUE_ROUND_DIGITS)))
        elif merged.geom_type == "MultiPolygon":
            for poly in merged.geoms:
                if not poly.is_empty:
                    grouped.append(Contour(poly, round(group_value, VALUE_ROUND_DIGITS)))

    return grouped


def parse_kml(path):
    tree = etree.parse(str(path))
    contours = []
    sources = {}
    styles = parse_styles(tree)

    for placemark in tree.xpath("//kml:Placemark", namespaces=NS):
        name_node = placemark.find("{%s}name" % NS["kml"])
        name_text = name_node.text if name_node is not None and name_node.text else ""

        if SAVE_SOURCES and is_source_placemark(placemark):
            sources[source_key(placemark)] = copy.deepcopy(placemark)

        if "Contour Level" not in name_text:
            continue

        match = PAT_NUMBER.search(name_text)

        if not match:
            continue

        value = float(match.group())

        for poly_node in placemark.xpath(".//kml:Polygon", namespaces=NS):
            geom = make_polygon_from_kml_polygon(poly_node)

            if geom is not None and not geom.is_empty:
                contours.append(Contour(geom, value))

    contours_before_group = len(contours)
    contours = group_contours_before_overlay(contours)
    return contours, sources, styles, contours_before_group


def build_flat_index(layers):
    geoms = []
    values = []

    for layer in layers:
        for contour in layer:
            if contour.geom is not None and not contour.geom.is_empty:
                geoms.append(contour.geom)
                values.append(contour.value)

    if not geoms:
        return [], [], None, {}

    tree = STRtree(geoms)
    geom_id_to_index = {id(geom): idx for idx, geom in enumerate(geoms)}
    return geoms, values, tree, geom_id_to_index


def query_candidate_indexes(tree, geoms, geom_id_to_index, point):
    """
    Совместимость с Shapely 1.x и 2.x.
    Shapely 2.x возвращает индексы, Shapely 1.x — геометрии.
    """
    raw = tree.query(point)
    candidates = []

    for item in raw:
        if hasattr(item, "__int__") and not hasattr(item, "geom_type"):
            candidates.append(int(item))
        else:
            idx = geom_id_to_index.get(id(item))
            if idx is not None:
                candidates.append(idx)

    return candidates


def objects_to_layer(objects):
    layer = []

    for geom, value in objects:
        if geom is None or geom.is_empty:
            continue

        if geom.geom_type == "Polygon":
            layer.append(Contour(geom, value))
        elif geom.geom_type == "MultiPolygon":
            for poly in geom.geoms:
                if not poly.is_empty:
                    layer.append(Contour(poly, value))

    return layer


def build_sum_overlay(layers):
    """
    Строит overlay и суммирует значения концентрации.
    Деление на количество KML выполняется только один раз в конце.
    """
    geoms, values, tree, geom_id_to_index = build_flat_index(layers)

    if tree is None:
        return []

    boundaries = [geom.boundary for geom in geoms]

    if not boundaries:
        return []

    union_boundaries = unary_union(boundaries)
    cells = polygonize(union_boundaries)
    result = []

    for cell in cells:
        if cell.is_empty:
            continue

        if MIN_CELL_AREA > 0 and cell.area < MIN_CELL_AREA:
            continue

        point = cell.representative_point()
        concentration_sum = 0.0

        for idx in query_candidate_indexes(tree, geoms, geom_id_to_index, point):
            if geoms[idx].covers(point):
                concentration_sum += values[idx]

        if concentration_sum > 0:
            result.append((cell, round(concentration_sum, VALUE_ROUND_DIGITS)))

    return result


def merge_geometries(geoms):
    if not geoms:
        return None

    merged = unary_union(geoms)
    merged = clean_geom(merged)

    if FILL_POLYGON_HOLES and merged is not None:
        merged = remove_holes(merged)
        merged = clean_geom(merged)

    return merged


def merge_by_exact_value(objects):
    groups = {}

    for geom, value in objects:
        if geom is not None and not geom.is_empty:
            groups.setdefault(value, []).append(geom)

    merged_objects = []

    for value, geoms in groups.items():
        merged = merge_geometries(geoms)

        if merged is not None and not merged.is_empty:
            merged_objects.append((merged, value))

    return sorted(merged_objects, key=lambda x: x[1])


def process_batch(batch_files):
    batch_start = time.perf_counter()
    read_start = time.perf_counter()

    layers = []
    batch_sources = {}
    batch_styles = {}
    total_contours_before_group = 0
    total_contours_after_group = 0

    for file in batch_files:
        file_start = time.perf_counter()
        contours, sources, styles, contours_before_group = parse_kml(file)
        file_time = time.perf_counter() - file_start

        total_contours_before_group += contours_before_group
        total_contours_after_group += len(contours)

        print(
            f"  {file.name}: контуров={contours_before_group} -> {len(contours)}, "
            f"источников={len(sources)}, стилей={len(styles)}, "
            f"чтение={format_seconds(file_time)}",
        )

        if contours:
            layers.append(contours)

        batch_sources.update(sources)

        for style_id, style_node in styles.items():
            if style_id not in batch_styles:
                batch_styles[style_id] = style_node

    read_time = time.perf_counter() - read_start

    if not layers:
        total_time = time.perf_counter() - batch_start
        timings = {
            "read": read_time,
            "overlay": 0.0,
            "merge": 0.0,
            "total": total_time,
            "contours_before_group": total_contours_before_group,
            "contours_after_group": total_contours_after_group,
            "overlay_cells": 0,
        }
        return [], batch_sources, batch_styles, timings

    overlay_start = time.perf_counter()
    batch_sum_objects = build_sum_overlay(layers)
    overlay_time = time.perf_counter() - overlay_start
    overlay_cells = len(batch_sum_objects)

    merge_start = time.perf_counter()
    batch_sum_objects = merge_by_exact_value(batch_sum_objects)
    merge_time = time.perf_counter() - merge_start

    total_time = time.perf_counter() - batch_start
    timings = {
        "read": read_time,
        "overlay": overlay_time,
        "merge": merge_time,
        "total": total_time,
        "contours_before_group": total_contours_before_group,
        "contours_after_group": total_contours_after_group,
        "overlay_cells": overlay_cells,
    }

    return batch_sum_objects, batch_sources, batch_styles, timings


def add_batch_to_accumulated(accumulated_objects, batch_objects):
    if not accumulated_objects:
        return batch_objects

    if not batch_objects:
        return accumulated_objects

    accumulated_layer = objects_to_layer(accumulated_objects)
    batch_layer = objects_to_layer(batch_objects)

    summed = build_sum_overlay([accumulated_layer, batch_layer])
    return merge_by_exact_value(summed)


def divide_to_average(objects, file_count):
    averaged = []

    for geom, value_sum in objects:
        avg_value = value_sum / file_count

        if avg_value > 0:
            averaged.append((geom, round(avg_value, VALUE_ROUND_DIGITS)))

    return averaged


def concentration_group_key(value):
    if value <= 0:
        return None

    log_value = math.log10(value)
    return math.floor(log_value / LOG10_GROUP_STEP) * LOG10_GROUP_STEP


def calculate_group_value(items):
    values = [value for geom, value in items]

    if GROUP_VALUE_METHOD == "max":
        return max(values)

    if GROUP_VALUE_METHOD == "arithmetic":
        return sum(values) / len(values)

    weighted_sum = 0.0
    area_sum = 0.0

    for geom, value in items:
        area = geom.area if geom is not None else 0.0
        weighted_sum += value * area
        area_sum += area

    if area_sum > 0:
        return weighted_sum / area_sum

    return sum(values) / len(values)


def merge_close_levels_by_order(objects):
    if not MERGE_CLOSE_LEVELS or LOG10_GROUP_STEP <= 0:
        return merge_by_exact_value(objects)

    groups = {}

    for geom, value in objects:
        key = concentration_group_key(value)

        if key is None:
            continue

        groups.setdefault(key, []).append((geom, value))

    merged_objects = []

    for _key, items in groups.items():
        geoms = [geom for geom, _value in items]
        merged = merge_geometries(geoms)

        if merged is None or merged.is_empty:
            continue

        group_value = calculate_group_value(items)
        merged_objects.append((merged, round(group_value, VALUE_ROUND_DIGITS)))

    return sorted(merged_objects, key=lambda x: x[1])


# =============================================================================
# LIMIT VERTICES BEFORE WRITING
# =============================================================================


def polygon_vertex_count(poly):
    count = len(poly.exterior.coords)
    count += sum(len(interior.coords) for interior in poly.interiors)
    return count


def geom_vertex_count(geom):
    if geom is None or geom.is_empty:
        return 0

    if geom.geom_type == "Polygon":
        return polygon_vertex_count(geom)

    if geom.geom_type == "MultiPolygon":
        return sum(polygon_vertex_count(poly) for poly in geom.geoms)

    return 0


def objects_vertex_count(objects):
    return sum(geom_vertex_count(geom) for geom, _value in objects)


def simplify_objects_for_kml(objects):
    if MAX_KML_VERTICES <= 0:
        return objects

    current_count = objects_vertex_count(objects)
    print("Вершин перед записью KML:", current_count)

    if current_count <= MAX_KML_VERTICES:
        return objects

    simplified = objects
    tolerance = WRITE_SIMPLIFY_START_TOL

    for iteration in range(1, WRITE_SIMPLIFY_MAX_ITERATIONS + 1):
        new_objects = []

        for geom, value in simplified:
            if geom is None or geom.is_empty:
                continue

            new_geom = geom.simplify(tolerance, preserve_topology=True)
            new_geom = clean_geom(new_geom)

            if FILL_POLYGON_HOLES and new_geom is not None:
                new_geom = remove_holes(new_geom)
                new_geom = clean_geom(new_geom)

            if new_geom is not None and not new_geom.is_empty:
                new_objects.append((new_geom, value))

        simplified = merge_by_exact_value(new_objects)
        current_count = objects_vertex_count(simplified)

        print(
            f"  Упрощение перед записью {iteration}: tolerance={tolerance:.8f}, вершин={current_count}",
        )

        if current_count <= MAX_KML_VERTICES:
            return simplified

        tolerance *= WRITE_SIMPLIFY_GROWTH

    print("  ⚠ Не удалось снизить число вершин ниже лимита, KML всё равно будет записан.")
    return simplified


# =============================================================================
# COLORS / WRITE KML
# =============================================================================


def interpolate_color(value, vmin, vmax):
    """Возвращает KML-цвет AABBGGRR: зелёный -> жёлтый -> оранжевый -> красный."""

    if vmax == vmin:
        t = 1.0
    else:
        t = (value - vmin) / (vmax - vmin)

    t = max(0.0, min(1.0, t))

    stops = [
        (0.00, (0, 160, 0)),
        (0.25, (0, 255, 0)),
        (0.50, (255, 255, 0)),
        (0.75, (255, 140, 0)),
        (1.00, (255, 0, 0)),
    ]

    r = g = b = 0

    for i in range(len(stops) - 1):
        t0, color0 = stops[i]
        t1, color1 = stops[i + 1]

        if t0 <= t <= t1:
            k = (t - t0) / (t1 - t0) if t1 != t0 else 0.0
            r = int(color0[0] + (color1[0] - color0[0]) * k)
            g = int(color0[1] + (color1[1] - color0[1]) * k)
            b = int(color0[2] + (color1[2] - color0[2]) * k)
            break

    return f"{KML_ALPHA}{b:02x}{g:02x}{r:02x}"


def add_concentration_style(doc, style_id, color):
    style = etree.SubElement(doc, "Style", id=style_id)

    line_style = etree.SubElement(style, "LineStyle")
    etree.SubElement(line_style, "color").text = "00000000"
    etree.SubElement(line_style, "width").text = "0"

    poly_style = etree.SubElement(style, "PolyStyle")
    etree.SubElement(poly_style, "color").text = color
    etree.SubElement(poly_style, "fill").text = "1"
    etree.SubElement(poly_style, "outline").text = "0"


def append_unique_styles(doc, styles):
    for style_node in styles.values():
        doc.append(copy.deepcopy(style_node))


def write_polygon(parent, poly):
    polygon_node = etree.SubElement(parent, "Polygon")

    outer = etree.SubElement(polygon_node, "outerBoundaryIs")
    outer_ring = etree.SubElement(outer, "LinearRing")
    outer_coords = etree.SubElement(outer_ring, "coordinates")
    outer_coords.text = " ".join(f"{x:.8f},{y:.8f},0" for x, y in poly.exterior.coords)

    if not FILL_POLYGON_HOLES:
        for interior in poly.interiors:
            inner = etree.SubElement(polygon_node, "innerBoundaryIs")
            inner_ring = etree.SubElement(inner, "LinearRing")
            inner_coords = etree.SubElement(inner_ring, "coordinates")
            inner_coords.text = " ".join(f"{x:.8f},{y:.8f},0" for x, y in interior.coords)


def write_kml(objects, sources, source_styles, output_file):
    objects = simplify_objects_for_kml(objects)

    root = etree.Element("kml", nsmap={None: NS["kml"]})
    doc = etree.SubElement(root, "Document")

    etree.SubElement(doc, "name").text = "Average concentration field"

    if SAVE_SOURCES:
        append_unique_styles(doc, source_styles)

    if objects:
        values = sorted({value for _, value in objects})
        vmin = min(values)
        vmax = max(values)

        style_by_value = {}

        for idx, value in enumerate(values):
            style_id = f"avg_conc_{idx}"
            style_by_value[value] = style_id
            add_concentration_style(doc, style_id, interpolate_color(value, vmin, vmax))

        folder = etree.SubElement(doc, "Folder")
        etree.SubElement(folder, "name").text = "Average concentration zones grouped by order"

        for geom, value in objects:
            placemark = etree.SubElement(folder, "Placemark")
            etree.SubElement(placemark, "name").text = f"Average concentration: {value:.6E}"
            etree.SubElement(placemark, "styleUrl").text = "#" + style_by_value[value]
            etree.SubElement(placemark, "description").text = (
                f"Average concentration: {value:.12E}\n"
                "Color scale: green = minimum, red = maximum\n"
                "Calculation: accumulated concentration sum divided by number of KML files\n"
                f"Batch size: {BATCH_SIZE}\n"
                f"Pre simplify tolerance: {PRE_SIMPLIFY_TOL}\n"
                f"Fill polygon holes: {FILL_POLYGON_HOLES}\n"
                f"Close levels grouped by log10 step: {LOG10_GROUP_STEP}"
            )

            multi = etree.SubElement(placemark, "MultiGeometry")

            if geom.geom_type == "Polygon":
                polygons = [geom]
            elif geom.geom_type == "MultiPolygon":
                polygons = list(geom.geoms)
            else:
                polygons = []

            for poly in polygons:
                if not poly.is_empty:
                    write_polygon(multi, poly)

    if SAVE_SOURCES and sources:
        sources_folder = etree.SubElement(doc, "Folder")
        etree.SubElement(sources_folder, "name").text = "Unique emission sources from original KML"

        for source_node in sources.values():
            sources_folder.append(copy.deepcopy(source_node))

    etree.ElementTree(root).write(
        str(output_file),
        pretty_print=True,
        xml_declaration=True,
        encoding="UTF-8",
    )


# =============================================================================
# MAIN
# =============================================================================


def find_input_files():
    files = []

    for path in sorted(INPUT_DIR.glob("*.kml")):
        if path.name == OUTPUT_FILE.name:
            continue
        if path.name.startswith("AVERAGE_CONCENTRATION"):
            continue
        files.append(path)

    return files


def main():
    total_start = time.perf_counter()
    files = find_input_files()

    if not files:
        raise FileNotFoundError(f"KML файлы не найдены в папке: {INPUT_DIR.resolve()}")

    total_files = len(files)
    total_batches = (total_files + BATCH_SIZE - 1) // BATCH_SIZE

    print("Найдено KML-файлов:", total_files)
    print("Размер батча:", BATCH_SIZE)
    print("Всего батчей:", total_batches)
    print("Предварительное упрощение:", PRE_SIMPLIFY_TOL)
    print("Предварительная группировка до overlay:", PRE_GROUP_CLOSE_LEVELS_BEFORE_OVERLAY)
    print("Заполнение дыр внутри полигонов:", FILL_POLYGON_HOLES)
    print("Лимит вершин итогового KML:", MAX_KML_VERTICES)
    print("Группировка близких уровней:", MERGE_CLOSE_LEVELS)
    print("Шаг группировки log10:", LOG10_GROUP_STEP)
    print("Промежуточные файлы: не сохраняются")

    accumulated_sum_objects = []
    all_sources = {}
    all_styles = {}

    for batch_index, batch_files in enumerate(chunked(files, BATCH_SIZE), start=1):
        processed_before = min((batch_index - 1) * BATCH_SIZE, total_files)
        files_left_before = total_files - processed_before
        batches_left_after = total_batches - batch_index

        print()
        print(f"=== Батч {batch_index}/{total_batches}: файлов={len(batch_files)} ===")
        print(f"Осталось файлов перед батчем: {files_left_before}")

        batch_total_start = time.perf_counter()
        batch_objects, batch_sources, batch_styles, batch_timings = process_batch(batch_files)

        all_sources.update(batch_sources)

        for style_id, style_node in batch_styles.items():
            if style_id not in all_styles:
                all_styles[style_id] = style_node

        print("  Контуров в батче до группировки:", batch_timings["contours_before_group"])
        print("  Контуров в батче после группировки:", batch_timings["contours_after_group"])
        print("  Элементарных областей overlay батча:", batch_timings["overlay_cells"])
        print("  Областей суммы в батче после объединения:", len(batch_objects))
        print("  Время чтения батча:", format_seconds(batch_timings["read"]))
        print("  Время overlay батча:", format_seconds(batch_timings["overlay"]))
        print("  Время объединения батча:", format_seconds(batch_timings["merge"]))

        accumulate_start = time.perf_counter()
        accumulated_sum_objects = add_batch_to_accumulated(accumulated_sum_objects, batch_objects)
        accumulate_time = time.perf_counter() - accumulate_start
        batch_full_time = time.perf_counter() - batch_total_start

        print("  Областей накопленной суммы:", len(accumulated_sum_objects))
        print("  Время накопления результата:", format_seconds(accumulate_time))
        print("  Полное время батча:", format_seconds(batch_full_time))
        print("  Осталось батчей после текущего:", batches_left_after)

    if not accumulated_sum_objects:
        raise ValueError("Не найдено контуров вида 'Contour Level: ...'")

    final_start = time.perf_counter()
    average_start = time.perf_counter()
    average_objects = divide_to_average(accumulated_sum_objects, total_files)
    exact_merged_objects = merge_by_exact_value(average_objects)
    average_time = time.perf_counter() - average_start

    print()
    print("Уникальных средних концентраций до группировки:", len(exact_merged_objects))

    grouping_start = time.perf_counter()
    grouped_objects = merge_close_levels_by_order(average_objects)
    grouping_time = time.perf_counter() - grouping_start

    print("Уникальных средних концентраций после группировки:", len(grouped_objects))
    print("Уникальных источников:", len(all_sources))

    write_start = time.perf_counter()
    write_kml(grouped_objects, all_sources, all_styles, OUTPUT_FILE)
    write_time = time.perf_counter() - write_start
    final_time = time.perf_counter() - final_start
    total_time = time.perf_counter() - total_start

    print("Время усреднения:", format_seconds(average_time))
    print("Время финальной группировки:", format_seconds(grouping_time))
    print("Время записи KML:", format_seconds(write_time))
    print("Время финального этапа:", format_seconds(final_time))
    print("Общее время процесса:", format_seconds(total_time))
    print(f"Готово: {OUTPUT_FILE.resolve()}")


if __name__ == "__main__":
    main()
