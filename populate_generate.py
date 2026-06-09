from parse_source_file import parser_source_file
from populate_module import generate_concentrations

if __name__ == "__main__":
    # project_dir = "D:\\work\\python\\Meteorology_works\\Hysplit_model_dispersion"

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

    # basename = "Angarsk"
    # coordinates = (52.25, 104.2)
    # years = [2005]
    # months = [4]
    # hours = [0]
    # altitudes = [215]
    # run = 12
    # emission_rate = 4780000000
    # hours_emission = 12
    # method = 0
    # top_model_domain = 10000.0
    #
    # generate_concentrations(
    #     basename=basename,
    #     hysplit_working=hysplit_dir,
    #     output_dir=output_dir,
    #     meteo_dir=meteo_dir,
    #     years=years,
    #     months=months,
    #     hours=hours,
    #     altitudes=altitudes,
    #     coordinates=coordinates,
    #     run=run,
    #     emission_rate=emission_rate,
    #     hours_emission=hours_emission,
    #     method=method,
    #     top_model_domain=top_model_domain,
    # )
