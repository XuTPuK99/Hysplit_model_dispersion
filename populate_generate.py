from parse_source_file import parser_source_file
from populate_module import generate_concentrations

if __name__ == "__main__":
    hysplit_dir = r"C:\hysplit\working"
    output_dir = r"D:\work\python\Meteorology_works\Hysplit_model_dispersion\result_kml"
    meteo_dir = r"D:\meteodata"

    input_path_source_file = r"D:\work\python\Meteorology_works\Hysplit_model_dispersion\source_file.txt"

    sources = parser_source_file(input_path_source_file)

    for source in sources:
        generate_concentrations(
            basename=source.basename,
            hysplit_working=hysplit_dir,
            output_dir=output_dir,
            meteo_dir=meteo_dir,
            years=source.years,
            months=source.months,
            hours=source.hours,
            altitudes=source.altitudes,
            coordinates=source.coordinates,
            run=source.run,
            emission_rate=source.emission_rate,
            hours_emission=source.hours_emission,
            method=source.method,
        )
