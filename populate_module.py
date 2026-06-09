from __future__ import division

import fnmatch
import itertools
import os
import shutil
from calendar import monthrange
from subprocess import call


def generate_concentrations(
    basename,
    hysplit_working,
    output_dir,
    meteo_dir,
    years,
    months,
    hours,
    altitudes,
    coordinates,
    run,
    emission_rate,
    hours_emission,
    method,
    top_model_domain=10000.0,
    meteoyr_2digits=True,
    outputyr_2digits=False,
    monthslice=slice(0, 32, 1),
    meteo_bookends=([4, 5], [1]),
    hysplit_std="C:\\hysplit\\exec\\hycs_std",
    hysplit_conc="C:\\hysplit\\exec\\concplot",
    base_filename="HYSPLIT_ps.kml",
):
    """
    Generate sequence of trajectories within given time frame(s).

    Run bulk sequence of HYSPLIT simulations over a given time and at different
    altitudes (likely in meters above ground level).  Uses either weekly or
    semi-monthly data with the filename format of *mon*YY*# or *mon*YYYY*#.
    Results are written to ``output_dir``.

    This does not set along-trajectory meteorological output- edit SETUP.CFG
    in the HYSPLIT working directory or in the HYSPLIT GUI to reflect
    desired output variables.

    Absolute paths strongly recommended over relative paths.

    Parameters
    ----------
    basename : string
        Base for all files output in this run
    hysplit_working : string
        Absolute or relative path to the HYSPLIT working directory.
    output_dir : string
        Absolute or relative path to the desired output directory.
    meteo_dir : string
        Absolute or relative path to the location of the meteorology files.
    years : list of ints
        The year(s) to run simulations
    months : list of ints
        The month(s) to run simulations
    hours : list of ints
        Parcel launching times in UTC.
    altitudes : list of ints
        The altitudes (usually meters above ground level) from which
        parcels will be launched.  Must be less than model top (10000 m)
    coordinates : tuple of floats
        The parcel (latitude, longitude) launch location in decimal degrees.
    run : int
        Length in hours of simulation.  To calculate back trajectories,
        ``run`` must be negative.
    meteoyr_2digits : Boolean
        Default True.  Indicates whether to search for meteorology files using
        the last 2 or all 4 digits of the years.  Must set to False if have
        multiple decades of meteorology files in meteo_dir.
    outputyr_2digits : Boolean
        Default False.  Old behavior == True.  The number of digits (2 or 4) to
        use to identify year in trajectory filename.  Must keep as False if
        wish PySPLIT to correctly identify non-21st century trajectories later
    monthslice : slice object
        Default slice(0, 32, 1).  Slice to apply to range of days in month.
        Use to target particular day or range of days, every x number of days,
        etc.  NOTE: slice is 0 indexed, days start with 1.  For example,
        slice(0, 32, 2) will yield every odd day.
    meteo_bookends : tuple of lists of ints
        Default ([4, 5], [1]).  To calculate a month of trajectories, files
        from the previous and month must be included.  The default is optimized
        for weekly meteorology and indicates that weeks 4 and 5 from the
        previous month and the first week of the next month must be included
        to run the entire current month of trajectories.  The user is
        responsible for making sure the correct bookends for their trajectory
        length and meteorology file periods are provided.
    hysplit_std : string
        Default "C:\\hysplit\\exec\\hycs_std.exe".  The location of the "hycs_std.exe"
        executable that generates trajectories.  This is the default location
        for a typical PC installation of HYSPLIT

    """
    # Set year formatting in 3 places
    yr_is2digits = {True: _year2string, False: str}

    controlyearfunc = yr_is2digits[True]
    meteoyearfunc = yr_is2digits[meteoyr_2digits]
    fnameyearfunc = yr_is2digits[outputyr_2digits]

    if outputyr_2digits is False or meteoyr_2digits is False:
        for year in years:
            if len(str(year)) != 4:
                raise ValueError("%d is not a valid year for given meteoyr_2digits, outputyr_2digits" % year)

    controlfname = "CONTROL"

    # Get directory information, make directories if necessary
    cwd = os.getcwd()

    if not os.path.isdir(output_dir):
        os.mkdir(output_dir)

    meteo_dir = meteo_dir.replace("\\", "/")

    # Initialize dictionary of months, seasons
    n_hemisphere = True
    if coordinates[0] < 0:
        n_hemisphere = False

    mon_dict = _mondict(n_hem=n_hemisphere)

    try:
        os.chdir(hysplit_working)

        # Iterate over years and months
        for y, m in itertools.product(years, months):
            season = mon_dict[m][0]
            m_str = mon_dict[m][1]
            m_len = monthrange(y, m)[1]

            days = range(1, m_len + 1)[monthslice]

            # Assemble list of meteorology files
            meteofiles = _meteofinder(meteo_dir, meteo_bookends, m, y, mon_dict, meteoyearfunc)

            controlyr = controlyearfunc(y)
            fnameyr = fnameyearfunc(y)

            # Iterate over days, hours, altitudes
            for d, h, a in itertools.product(days, hours, altitudes):
                # Add timing and altitude to basename to create unique name
                filename = (
                    basename
                    + m_str
                    + "{:04}".format(a)
                    + season
                    + fnameyr
                    + "{0:02}{1:02}{2:02}".format(m, d, h)
                    + ".kml"
                )

                final_filename_path = os.path.join(output_dir, filename)

                # Remove any existing CONTROL or temp files
                _try_to_remove(controlfname)
                _try_to_remove(filename)
                _try_to_remove(final_filename_path)

                # Populate CONTROL file with trajectory initialization data
                _populate_control(
                    coords=coordinates,
                    year=controlyr,
                    month=m,
                    day=d,
                    hour=h,
                    alt=a,
                    meteo_dir=meteo_dir,
                    meteofiles=meteofiles,
                    run=run,
                    emission_rate=emission_rate,
                    hours_emission=hours_emission,
                    method=method,
                    top_model_domain=top_model_domain,
                    controlfname=controlfname,
                )

                # Call executable to calculate trajectory
                call(hysplit_std)
                call([hysplit_conc, "-a3", "-i./cdump", "-jC:/hysplit/graphics/arlmap"])

                # Move the trajectory file to output directory
                shutil.move(base_filename, final_filename_path)

    # Revert current working directory
    finally:
        os.chdir(cwd)


def _meteofinder(meteo_dir, meteo_bookends, mon, year, mon_dict, meteoyearfunc):
    """
    Get list of meteorology files.

    Creates list of files in storage location ``meteo_dir`` that belong
    to the given month and year, plus the necessary files from previous
    and the next months (``meteo_bookends``).

    For successful meteofinding, separate different meteorology types into
    different folders and name weekly or semi-monthly files according to the
    following convention:
        *mon*YY*#
    where the * represents a Bash wildcard.

    Parameters
    ----------
    meteo_dir : string
        Full or relative path to the location of the meteorology files.
    meteo_bookends : tuple of lists of ints
        To calculate a month of trajectories, files from the previous and next
        month must be included.  This indicates which file numbers from the
        previous month and which from the next month are necessary.
        The user is responsible for making sure the correct bookends for their
        trajectory length and meteorology file periods are provided.
    mon : int
        The integer representation of the current month.  Converted to a
        3-letter string to find meteorology files.
    year : int
        The integer representation of the current year.  Converted to a length
        2 string to find meteorology files.
    mon_dict : dictionary
        Dictionary keyed by month integer, with lists of [season, mon]
    meteoyearfunc : function
        Function that formats the year string to length 2 or 4 to identify
        appropriate meteorology files

    Returns
    -------
    meteofiles : list of strings
        List of strings representing the names of the required
        meteorology files

    """
    # Current working directory set in generate_bulktraj() environment
    orig_dir = os.getcwd()

    # Initialize lists, count
    meteofiles = []
    file_number = -1

    # Get the strings that will match files for the previous, next,
    # and current months
    prv, nxt, now = _monyearstrings(mon, year, mon_dict, meteoyearfunc)

    # Change directory and walk through files
    try:
        os.chdir(meteo_dir)

        _, _, files = next(os.walk("."))

        # Order of files to CONTROL doesn't matter
        for each_file in files:
            if fnmatch.fnmatch(each_file, now):
                meteofiles.append(each_file)
            elif fnmatch.fnmatch(each_file, prv):
                if int(each_file[file_number]) in meteo_bookends[0]:
                    meteofiles.append(each_file)
            elif fnmatch.fnmatch(each_file, nxt):
                if int(each_file[file_number]) in meteo_bookends[1]:
                    meteofiles.append(each_file)

    finally:
        os.chdir(orig_dir)

    num_files = len(meteofiles)

    if num_files == 0:
        raise OSError("0 files found for month/year %(mon)d / %(year)d" % {"mon": mon, "year": year})

    if num_files > 12:
        print(meteofiles)
        raise OSError(
            "%(f)d files found for month/year %(mon)d / %(year)d."
            "  Maximum 12 allowed.  If wrong years are included, "
            "identify files by 4 digit years (meteoyr_2digits=True)."
            "  May require renaming meteorology files." % {"f": num_files, "mon": mon, "year": year},
        )

    return meteofiles


# this is the modified feature
def _populate_control(
    coords,
    year,
    month,
    day,
    hour,
    alt,
    meteo_dir,
    meteofiles,
    run,
    emission_rate,
    hours_emission,
    id_pollutant="TEST",
    controlfname="CONTROL",
    method=0,
    concname="cdump",
    top_model_domain=10000.0,
):
    r"""
    Initialize and write CONTROL from concentrate text to file (called CONTROL).

    Parameters
    ----------
    coordinates : tuple of floats
        The parcel (latitude, longitude) launch location in decimal degrees.
    years : list of ints
        The year of the simulation
    months : list of ints
        The month of the simulation
    hours : list of ints
        Parcel launching times in UTC.
    alt : int
        The altitude (usually meters above ground level) from which
        parcel will be launched.  Must be less than model top (10000 m)
    meteo_dir : string
        Full or relative path to the location of the meteorology files.
    meteofiles : list of strings
        List of strings representing the names of the required
        meteorology files
    run : int
        Length in hours of simulation.
    method : int
        Indicates the vertical motion calculation method.
        Vertical motion option (0:data 1:isob 2:isen 3:dens 4:sigma 5:diverg
                                6:msl2agl 7:average 9: fix-up&down 10: fixdown)
    emission_rate : int
        Mass units released each hour (l\hr).
    hours_emission : int
        The duration of emission may be defined in fractional hours
    id_pollutant : string
        Provides a four-character label that can be used to identify the pollutant.
        The number of characters is MOST REQUIRED <= 4.
    controlfname : string
        The name of the control file, which should be 'CONTROL'
    top_model_domain : float
        Sets the vertical limit of the internal meteorological grid.
        If calculations are not required above a certain level,
        fewer meteorological data are processed thus speeding up the computation.
        Defoult = 10000.0, Max = 25000.0
    """

    # if len(id_pollutant) > 4:
    #    print("Error: id_pollutant size > 4")
    #    return None

    # it is changes function
    controltext = [
        year + " {0:02} {1:02} {2:02}\n".format(month, day, hour),
        "1\n",
        "{0!s} {1!s} {2!s}\n".format(coords[0], coords[1], alt),
        "{0!s}\n".format(run),
        f"{method}\n",
        f"{top_model_domain}\n",
        "{0!s}\n".format(len(meteofiles)),
    ]

    for fname in meteofiles:
        controltext.append("{0}/\n".format(meteo_dir))
        controltext.append("{0}\n".format(fname))

    # Pollutant
    controltext.append("1\n")
    controltext.append(f"{id_pollutant}\n")
    controltext.append(f"{emission_rate}\n")
    controltext.append(f"{hours_emission}\n")
    controltext.append("00 00 00 00 00\n")

    # Grid
    controltext.append("1\n")
    controltext.append("0.0 0.0\n")
    controltext.append("0.05 0.05\n")
    controltext.append("30.0 30.0\n")
    controltext.append("./\n")
    controltext.append("{0}\n".format(concname))  # cdump
    controltext.append("1\n")
    controltext.append("100\n")
    controltext.append("00 00 00 00 00\n")
    controltext.append("00 00 00 00 00\n")
    controltext.append("00 12 00\n")

    # Deposition
    controltext.append("1\n")
    controltext.append("0.0 0.0 0.0\n")
    controltext.append("0.03 0.0 0.0 0.0 0.0\n")
    controltext.append("0.0 0.0 0.0\n")
    controltext.append("0.0\n")
    controltext.append("0.0\n")

    with open(controlfname, "w") as control:
        control.writelines(controltext)


def _year2string(year):
    """
    Helper function, takes a four digit integer year, makes a length-2 string.

    Parameters
    ----------
    year : int
        The year.

    Returns
    -------
    Length-2 string representation of ``year``

    """
    return "{0:02}".format(year % 100)


def _monyearstrings(mon, year, mon_dict, meteoyearfunc):
    """
    Increment the months and potentially the years.

    Assemble the strings that will allow ``_meteofinder`` to get correct files.

    Parameters
    ----------
    mon : int
        Integer representation of the month
    year : int
        Integer representation of the year
    mon_dict : dictionary
        Dictionary keyed by month integer, with lists of [season, mon]
    meteoyearfunc : function
        Function that formats the year string to length 2 or 4 to identify
        appropriate meteorology files

    Returns
    -------
    prv : string
        Signature for gathering the meteorology files for the previous month
    nxt : string
        Signature for gathering the meteorology files for the next month
    now : string
        Signature for gathering the meteorology files for the current month

    """
    next_year = year
    prev_year = year

    next_mon = mon + 1
    prev_mon = mon - 1

    if prev_mon == 0:
        prev_mon = 12
        prev_year = year - 1
    if next_mon == 13:
        next_mon = 1
        next_year = year + 1

    w = "*"

    prv = w + mon_dict[prev_mon][1] + w + meteoyearfunc(prev_year) + w
    nxt = w + mon_dict[next_mon][1] + w + meteoyearfunc(next_year) + w
    now = w + mon_dict[mon][1] + w + meteoyearfunc(year) + w

    return prv, nxt, now


def _mondict(n_hem=True):
    """
    Get a dictionary of season and month string.

    Parameters
    ----------
    n_hem : Boolean
        Default True.  Indicates hemisphere of parcel launch and thus
        actual season.

    Returns
    -------
    season_month_dict : dictionary
        Dictionary keyed by month integer, with lists of [season, mon]

    """
    if n_hem:
        season_month_dict = {
            12: ["winter", "dec"],
            1: ["winter", "jan"],
            2: ["winter", "feb"],
            3: ["spring", "mar"],
            4: ["spring", "apr"],
            5: ["spring", "may"],
            6: ["summer", "jun"],
            7: ["summer", "jul"],
            8: ["summer", "aug"],
            9: ["autumn", "sep"],
            10: ["autumn", "oct"],
            11: ["autumn", "nov"],
        }
    else:
        season_month_dict = {
            12: ["summer", "dec"],
            1: ["summer", "jan"],
            2: ["summer", "feb"],
            3: ["autumn", "mar"],
            4: ["autumn", "apr"],
            5: ["autumn", "may"],
            6: ["winter", "jun"],
            7: ["winter", "jul"],
            8: ["winter", "aug"],
            9: ["spring", "sep"],
            10: ["spring", "oct"],
            11: ["spring", "nov"],
        }

    return season_month_dict


def _try_to_remove(string):
    """
    Check if file exists, and either remove it or pass.

    Parameters
    ----------
    string : string
        Name of file to attempt to remove

    """
    try:
        os.remove(string)
    except OSError:
        pass


def _day2filenum(interval, day):
    """
    Convert a date to corresponding file number.

    Results depend on file interval- weekly, daily, semi-monthly.

    Parameters
    ----------
    interval : string
        The file interval.  Daily, weekly, or semi-monthly accepted,
        represented by lower case first letter.
    day : string
        A number indicating the date.

    Returns
    -------
    filenum : string
        The number of the file within the month of meteorology.
    """
    if interval == "w":
        filenum = str(((int(day) - 1) // 7) + 1)
    elif interval == "s":
        filenum = str(((int(day) - 1) // 15) + 1)
    elif interval == "d" or interval == "m":
        filenum = day
    else:
        raise ValueError("Meteorology interval not recognized")

    return filenum
