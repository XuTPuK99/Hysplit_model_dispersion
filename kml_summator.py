"""
kml_summator_colored_merged_sources.py

Модифицированная версия kml_summator.py.

Изменения:
- все области с одинаковой суммарной концентрацией объединяются через unary_union;
- в итоговом KML на каждую концентрацию создаётся один Placemark с MultiGeometry;
- для каждой концентрации создаётся Style;
- цветовая шкала плавная: минимум — зелёный, максимум — красный;
- прозрачность плавно меняется: минимум более прозрачный, максимум менее прозрачный;
- в итоговый KML добавляются источники из исходных KML-файлов.

Требуется:
    pip install shapely lxml
"""

import copy
import re
from pathlib import Path

from lxml import etree
from shapely.geometry import Polygon
from shapely.ops import polygonize, unary_union
from shapely.strtree import STRtree

NS = {"kml": "http://www.opengis.net/kml/2.2"}
PAT = re.compile(r"[-+]?\d*\.?\d+(?:[Ee][-+]?\d+)?")


class Contour:
    def __init__(self, geom, value, style=None):
        self.geom = geom
        self.value = value
        self.style = style


def parse_coords(text):
    pts = []

    if not text:
        return pts

    for s in text.split():
        p = s.split(",")

        if len(p) >= 2:
            try:
                pts.append((float(p[0]), float(p[1])))
            except ValueError:
                pass

    return pts


def make_polygon_from_kml_polygon(poly_node):
    outer = poly_node.find(
        ".//{%s}outerBoundaryIs/{%s}LinearRing/{%s}coordinates" % (NS["kml"], NS["kml"], NS["kml"]),
    )

    if outer is None or outer.text is None:
        return None

    exterior = parse_coords(outer.text)

    if len(exterior) < 3:
        return None

    holes = []

    inner_nodes = poly_node.findall(
        ".//{%s}innerBoundaryIs/{%s}LinearRing/{%s}coordinates" % (NS["kml"], NS["kml"], NS["kml"]),
    )

    for inner in inner_nodes:
        if inner.text:
            ring = parse_coords(inner.text)
            if len(ring) >= 3:
                holes.append(ring)

    geom = Polygon(exterior, holes)

    if geom.is_empty:
        return None

    if not geom.is_valid:
        geom = geom.buffer(0)

    if PRE_SIMPLIFY_TOL > 0:
        try:
            geom = geom.simplify(PRE_SIMPLIFY_TOL, preserve_topology=True)
        except Exception:
            pass

    if not geom.is_valid:
        geom = geom.buffer(0)

    if geom.is_empty:
        return None

    return geom


def is_source_placemark(pm):
    """
    Определяет служебные точки источников/максимумов HYSPLIT.

    В разных KML они могут называться по-разному, поэтому проверяем:
    - имя;
    - styleUrl;
    - наличие Point.
    """

    name = pm.find("{%s}name" % NS["kml"])
    style_url = pm.find("{%s}styleUrl" % NS["kml"])
    point = pm.find(".//{%s}Point" % NS["kml"])

    name_text = (name.text or "").lower() if name is not None else ""
    style_text = (style_url.text or "").lower() if style_url is not None else ""

    source_words = (
        "source",
        "sorc",
        "emission",
        "release",
        "maximum value grid cell",
        "max value",
        "max",
        "источник",
        "выброс",
        "максим",
    )

    if any(word in name_text for word in source_words):
        return True

    if any(word in style_text for word in source_words):
        return True

    # HYSPLIT часто помечает источник стилем #sorc
    if "#sorc" in style_text:
        return True

    # Если это точка, но не Contour Level, также сохраняем её как служебный объект.
    if point is not None and "contour level" not in name_text:
        return True

    return False


def parse_styles(tree):
    """
    Сохраняет исходные стили и StyleMap, чтобы источники из исходных файлов
    отображались в итоговом KML так же, как в оригинале.
    """

    styles = []

    for node in tree.xpath("//kml:Style | //kml:StyleMap", namespaces=NS):
        node_id = node.get("id")

        if node_id:
            styles.append(copy.deepcopy(node))

    return styles


def parse_kml(path):
    """
    Возвращает:
    - contours: список Contour;
    - sources: список Placemark источников/служебных точек;
    - styles: список исходных Style/StyleMap.
    """

    tree = etree.parse(str(path))
    contours = []
    sources = []
    styles = parse_styles(tree)

    for pm in tree.xpath("//kml:Placemark", namespaces=NS):
        name = pm.find("{%s}name" % NS["kml"])
        name_text = name.text if name is not None and name.text else ""

        if is_source_placemark(pm):
            sources.append(copy.deepcopy(pm))

        if "Contour Level" not in name_text:
            continue

        m = PAT.search(name_text)

        if not m:
            continue

        value = float(m.group())

        style_url = pm.find("{%s}styleUrl" % NS["kml"])
        style = style_url.text if style_url is not None else None

        polys = pm.xpath(".//kml:Polygon", namespaces=NS)

        for p in polys:
            geom = make_polygon_from_kml_polygon(p)

            if geom is not None and not geom.is_empty:
                contours.append(Contour(geom, value, style))

    return contours, sources, styles


def build_flat_index(layers):
    geoms = []
    values = []

    for layer in layers:
        for contour in layer:
            if contour.geom is not None and not contour.geom.is_empty:
                geoms.append(contour.geom)
                values.append(contour.value)

    if not geoms:
        return [], [], None

    return geoms, values, STRtree(geoms)


def query_candidates(tree, geoms, point):
    """
    Совместимость с Shapely 1.x и 2.x:
    - Shapely 2.x возвращает индексы;
    - Shapely 1.x возвращает геометрии.
    """
    raw = tree.query(point)
    candidates = []

    for item in raw:
        if hasattr(item, "__int__") and not hasattr(item, "geom_type"):
            candidates.append(int(item))
        else:
            try:
                candidates.append(geoms.index(item))
            except ValueError:
                pass

    return candidates


def overlay(layers):
    boundaries = []

    for layer in layers:
        for contour in layer:
            if contour.geom is not None and not contour.geom.is_empty:
                boundaries.append(contour.geom.boundary)

    if not boundaries:
        return []

    geoms, values, tree = build_flat_index(layers)

    if tree is None:
        return []

    cells = polygonize(unary_union(boundaries))
    result = []

    for cell in cells:
        if cell.is_empty:
            continue

        point = cell.representative_point()
        concentration_sum = 0.0

        for idx in query_candidates(tree, geoms, point):
            if geoms[idx].covers(point):
                concentration_sum += values[idx]

        if concentration_sum > 0:
            result.append((cell, concentration_sum))

    return result


def merge(result):
    groups = {}

    for geom, value in result:
        groups.setdefault(value, []).append(geom)

    out = []

    for value, geoms in groups.items():
        merged = unary_union(geoms)

        # Сглаживание контуров:
        # buffer(+d) -> buffer(-d) убирает острые углы и мелкие выступы.
        try:
            d = max(merged.bounds[2] - merged.bounds[0], merged.bounds[3] - merged.bounds[1]) * 0.002

            if d > 0:
                merged = merged.buffer(d, join_style=1)
                merged = merged.buffer(-d, join_style=1)
        except Exception:
            pass

        if not merged.is_valid:
            merged = merged.buffer(0)

        # Дополнительное упрощение геометрии
        try:
            d2 = max(merged.bounds[2] - merged.bounds[0], merged.bounds[3] - merged.bounds[1]) * 0.0005

            if d2 > 0:
                merged = merged.simplify(d2, preserve_topology=True)
        except Exception:
            pass

        if merged.is_empty:
            continue

        out.append((merged, value))

    return sorted(out, key=lambda x: x[1])


def smooth_color_green_to_red(value, vmin, vmax, alpha_min=255, alpha_max=255):
    """
    Плавная шкала:
    минимум  -> зелёный, более прозрачный;
    середина -> жёлтый/оранжевый;
    максимум -> красный, менее прозрачный.

    Формат цвета KML: AABBGGRR.
    """

    if vmax == vmin:
        t = 1.0
    else:
        t = (value - vmin) / (vmax - vmin)

    t = max(0.0, min(1.0, t))

    # Расширенная цветовая шкала с большим количеством оттенков
    stops = [
        (0.00, (0, 120, 0)),
        (0.10, (0, 180, 0)),
        (0.20, (0, 255, 0)),
        (0.30, (120, 255, 0)),
        (0.40, (180, 255, 0)),
        (0.50, (255, 255, 0)),
        (0.60, (255, 220, 0)),
        (0.70, (255, 180, 0)),
        (0.80, (255, 120, 0)),
        (0.90, (255, 60, 0)),
        (1.00, (255, 0, 0)),
    ]

    for i in range(len(stops) - 1):
        t0, c0 = stops[i]
        t1, c1 = stops[i + 1]

        if t0 <= t <= t1:
            k = (t - t0) / (t1 - t0) if t1 != t0 else 0.0

            r = int(c0[0] + (c1[0] - c0[0]) * k)
            g = int(c0[1] + (c1[1] - c0[1]) * k)
            b = int(c0[2] + (c1[2] - c0[2]) * k)

            break
    else:
        r, g, b = stops[-1][1]

    alpha = int(alpha_min + (alpha_max - alpha_min) * t)

    return f"{alpha:02x}{b:02x}{g:02x}{r:02x}"


def add_concentration_style(doc, style_id, color):
    style = etree.SubElement(doc, "Style", id=style_id)

    # Границы полигонов отключены, чтобы не были видны линии между
    # соседними областями и зонами пересечений.
    line_style = etree.SubElement(style, "LineStyle")
    etree.SubElement(line_style, "color").text = "00000000"
    etree.SubElement(line_style, "width").text = "0"

    poly_style = etree.SubElement(style, "PolyStyle")
    etree.SubElement(poly_style, "color").text = color
    etree.SubElement(poly_style, "fill").text = "1"
    etree.SubElement(poly_style, "outline").text = "0"


def write_polygon(parent, poly):
    polygon_node = etree.SubElement(parent, "Polygon")

    outer = etree.SubElement(polygon_node, "outerBoundaryIs")
    outer_ring = etree.SubElement(outer, "LinearRing")
    outer_coords = etree.SubElement(outer_ring, "coordinates")

    outer_coords.text = " ".join(f"{x:.8f},{y:.8f},0" for x, y in poly.exterior.coords)

    for interior in poly.interiors:
        inner = etree.SubElement(polygon_node, "innerBoundaryIs")
        inner_ring = etree.SubElement(inner, "LinearRing")
        inner_coords = etree.SubElement(inner_ring, "coordinates")

        inner_coords.text = " ".join(f"{x:.8f},{y:.8f},0" for x, y in interior.coords)


def append_unique_styles(doc, styles):
    """
    Добавляет исходные Style/StyleMap без дублей по id.
    Это нужно, чтобы styleUrl у источников не потеряли оформление.
    """

    used_ids = set()

    for style in styles:
        sid = style.get("id")

        if not sid:
            continue

        if sid in used_ids:
            continue

        used_ids.add(sid)
        doc.append(copy.deepcopy(style))


def write_kml(objects, sources, source_styles, outfile):
    root = etree.Element("kml", nsmap={None: NS["kml"]})
    doc = etree.SubElement(root, "Document")

    etree.SubElement(doc, "name").text = "SUMMARY concentration overlay"

    # Сохраняем исходные стили, чтобы источники отображались корректно.
    append_unique_styles(doc, source_styles)

    if objects:
        values = sorted({value for _, value in objects})
        vmin = min(values)
        vmax = max(values)

        style_by_value = {}

        for idx, value in enumerate(values):
            style_id = f"summary_conc_{idx}"
            style_by_value[value] = style_id

            color = smooth_color_green_to_red(value, vmin, vmax)
            add_concentration_style(doc, style_id, color)

        concentrations_folder = etree.SubElement(doc, "Folder")
        etree.SubElement(concentrations_folder, "name").text = "Summed concentration zones"

        for geom, value in objects:
            pm = etree.SubElement(concentrations_folder, "Placemark")

            etree.SubElement(pm, "name").text = f"Contour Level: {value:.6E}"
            etree.SubElement(pm, "styleUrl").text = "#" + style_by_value[value]

            description = etree.SubElement(pm, "description")
            description.text = (
                f"Summed concentration level: {value:.6E}\n"
                "Color scale: green = minimum, red = maximum\n"
                "Opaque color scale: green = minimum, red = maximum"
            )

            multi = etree.SubElement(pm, "MultiGeometry")

            if geom.geom_type == "Polygon":
                geoms = [geom]
            elif geom.geom_type == "MultiPolygon":
                geoms = list(geom.geoms)
            else:
                geoms = []

            for poly in geoms:
                if not poly.is_empty:
                    write_polygon(multi, poly)

    # Добавляем источники из исходных KML.
    sources_folder = etree.SubElement(doc, "Folder")
    etree.SubElement(sources_folder, "name").text = "Sources from original KML"

    for source in sources:
        sources_folder.append(copy.deepcopy(source))

    etree.ElementTree(root).write(
        str(outfile),
        pretty_print=True,
        xml_declaration=True,
        encoding="UTF-8",
    )


def merge_fast(result):
    """
    Быстрое объединение для промежуточных батчей.

    Отличие от merge():
    - не выполняет buffer(+d)/buffer(-d);
    - не выполняет simplify();
    - только объединяет геометрии одинаковой концентрации.

    Это быстрее и подходит для промежуточных итераций.
    Финальное сглаживание остаётся в обычном merge().
    """
    groups = {}

    for geom, value in result:
        groups.setdefault(value, []).append(geom)

    out = []

    for value, geoms in groups.items():
        merged = unary_union(geoms)

        if not merged.is_valid:
            merged = merged.buffer(0)

        if merged.is_empty:
            continue

        out.append((merged, value))

    return sorted(out, key=lambda x: x[1])


# =============================================================================
# БАТЧЕВАЯ ОБРАБОТКА
# =============================================================================

BATCH_SIZE = 5

# Предварительное упрощение геометрии сразу после чтения.
# Уменьшает число вершин до overlay и ускоряет расчёт.
PRE_SIMPLIFY_TOL = 0.0005


def chunked(items, size):
    for i in range(0, len(items), size):
        yield items[i : i + size]


def objects_to_layer(objects):
    layer = []

    for geom, value in objects:
        if geom is None or geom.is_empty:
            continue

        if geom.geom_type == "Polygon":
            layer.append(Contour(geom, value))

        elif geom.geom_type == "MultiPolygon":
            for g in geom.geoms:
                if not g.is_empty:
                    layer.append(Contour(g, value))

    return layer


def parse_result_kml_as_layer(path):
    if not Path(path).exists():
        return []

    contours, _, _ = parse_kml(Path(path))
    return contours


def process_batch(files):
    layers = []
    sources_all = []
    styles_all = []

    for file in files:
        contours, sources, styles = parse_kml(file)

        print(
            f"{file.name}: контуров {len(contours)}, источников/служебных точек {len(sources)}, стилей {len(styles)}",
        )

        if contours:
            layers.append(contours)

        sources_all.extend(sources)
        styles_all.extend(styles)

    if not layers:
        return [], sources_all, styles_all

    res = overlay(layers)
    print("  Элементарных областей в батче:", len(res))

    merged = merge_fast(res)
    print("  Уникальных концентраций в батче:", len(merged))

    return merged, sources_all, styles_all


def sum_intermedia_with_batch(intermedia_file, batch_objects):
    if Path(intermedia_file).exists():
        print("  Чтение накопленного intermedia.kml")
        intermedia_layer = parse_result_kml_as_layer(intermedia_file)
    else:
        intermedia_layer = []

    batch_layer = objects_to_layer(batch_objects)

    if intermedia_layer and batch_layer:
        print("  Суммирование intermedia.kml + текущий батч")
        res = overlay([intermedia_layer, batch_layer])
        return merge_fast(res)

    if batch_layer:
        return batch_objects

    if intermedia_layer:
        return merge_fast((c.geom, c.value) for c in intermedia_layer)

    return []


def main():
    folder = Path(r"D:\work\python\Meteorology_works\Hysplit_model_dispersion\result_kml\data\Angarsk")
    output_file = "result.kml"

    files = sorted(folder.glob("*.kml"))

    if not files:
        raise FileNotFoundError(f"KML файлы не найдены в папке: {folder}")

    print("Файлов найдено:", len(files))
    print("Размер батча:", BATCH_SIZE)
    print("Предварительное упрощение:", PRE_SIMPLIFY_TOL)

    all_sources = []
    all_styles = []
    accumulated = []

    total_files = len(files)

    for batch_index, batch_files in enumerate(chunked(files, BATCH_SIZE), start=1):
        processed_before = min((batch_index - 1) * BATCH_SIZE, total_files)
        left_before = total_files - processed_before

        total_batches = (total_files + BATCH_SIZE - 1) // BATCH_SIZE
        batches_left_before = total_batches - batch_index + 1

        print()
        print(f"=== Батч {batch_index}/{total_batches}: файлов {len(batch_files)} ===")
        print(f"Осталось батчей (включая текущий): {batches_left_before}")
        print(f"Осталось файлов перед батчем: {left_before}")

        batch_objects, batch_sources, batch_styles = process_batch(batch_files)

        all_sources.extend(batch_sources)
        all_styles.extend(batch_styles)

        if accumulated and batch_objects:
            print("  Суммирование накопленного результата + текущий батч")
            accumulated_layer = objects_to_layer(accumulated)
            batch_layer = objects_to_layer(batch_objects)

            res = overlay([accumulated_layer, batch_layer])
            accumulated = merge_fast(res)

        elif batch_objects:
            accumulated = batch_objects

        print("  Уникальных суммарных концентраций после накопления:", len(accumulated))

        print("  Промежуточная запись отключена")

        processed_after = min(batch_index * BATCH_SIZE, total_files)
        left_after = total_files - processed_after

        batches_left_after = max(0, total_batches - batch_index)

        print(f"  Обработано файлов: {processed_after} из {total_files}")
        print(f"  Осталось файлов: {left_after}")
        print(f"  Осталось батчей: {batches_left_after}")

    print()
    print("Финальная запись итогового KML")

    final_objects = merge(accumulated)

    write_kml(
        final_objects,
        all_sources,
        all_styles,
        output_file,
    )

    print(f"{output_file} written")


if __name__ == "__main__":
    main()
