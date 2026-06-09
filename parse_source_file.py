import re
from dataclasses import dataclass


@dataclass()
class SourceData:
    basename: str | None = None
    coordinates: tuple[float] | None = None
    years: list[int] | None = None
    months: list[int] | None = None
    hours: list[int] | None = None
    altitudes: list[int] | None = None
    run: int | None = None
    emission_rate: int | None = None
    hours_emission: int | None = None
    method: int | None = None


def reg_func(regular: str, data: str) -> str | int | float | list[int] | tuple[float] | None:
    match = re.search(regular, data)
    if match:
        if "basename" in regular:
            return str(match[1])
        if "coordinates" in regular:
            return tuple(float(item) for item in match[1].split(", "))
        if "run" in regular or "emission_rate" in regular or "hours_emission" in regular or "method" in regular:
            return int(match[1])
        if "years" in regular or "months" in regular or "hours" in regular or "altitudes" in regular:
            return [int(item) for item in match[1].strip("[]").split(", ")]
    return None


def parser_source_file(path_to_sources_file: str) -> list:
    with open(path_to_sources_file, "r") as source:
        source_file = source.read()
    sources = re.split(r"(?:\r?\n){2,}", source_file.strip())

    result = []
    for source in sources:
        source_obj = SourceData()

        source_obj.basename = reg_func(r"basename\s*=\s*(.+)", source)
        source_obj.coordinates = reg_func(r"coordinates\s*=\s*\((.+)\)", source)
        source_obj.years = reg_func(r"years\s*=\s*(.+)", source)
        source_obj.months = reg_func(r"months\s*=\s*(.+)", source)
        source_obj.hours = reg_func(r"hours\s*=\s*(.+)", source)
        source_obj.altitudes = reg_func(r"altitudes\s*=\s*(.+)", source)
        source_obj.run = reg_func(r"run\s*=\s*(.+)", source)
        source_obj.emission_rate = reg_func(r"emission_rate\s*=\s*(.+)", source)
        source_obj.hours_emission = reg_func(r"hours_emission\s*=\s*(.+)", source)
        source_obj.method = reg_func(r"method\s*=\s*(.+)", source)

        result.append(source_obj)
    return result
