"""Tests for wind.py against generated spreadsheets.

REAL_LAYOUT copies the shape of the City's real file. Other layouts are variations the parser
is meant to cope with. The opt-in live test at the end checks the real file itself.
"""
import io
import os
import zipfile

import numpy as np
import pandas as pd
import pytest

from wind import (
    CACHE_FILE,
    ODS_MIMETYPE,
    attach_wind,
    load_wind,
    parse_wind,
    validate_spreadsheet,
)

HEADER = ["Date & Time", "Wind Speed (m/s)", "Wind Direction (deg)"]

REAL_LAYOUT = [
    ["MultiStation:  Periodically: 01/01/2020 00:00-31/12/2020 23:59  Type: AVG 1 Hr.",
     None, None, None, None],
    [None, None, None, None, None],
    ["Date & Time", "Atlantis AQM Site", "Atlantis AQM Site", "Goodwood AQM Station", "Goodwood AQM Station"],
    [None, "Wind Dir V", "Wind Speed V", "Wind Dir V", "Wind Speed V"],
    [None, "Deg", "m/s", "Deg", "m/s"],
    ["01/02/2020 00:00", 173.0, 4.1, 247.8, 19.2],
    ["01/02/2020 01:00", "NoData", "NoData", 247.0, 17.9],
    ["13/02/2020 02:00", "Down", "Down", 246.4, 17.1],
    ["13/02/2020 03:00", "<Samp", "<Samp", 245.1, 15.7],
    ["Minimum", 173.0, 4.1, 245.1, 15.7],
    ["MinDate", "01/02/2020 00:00", "01/02/2020 00:00", "13/02/2020 03:00", "13/02/2020 03:00"],
    ["Avg", 173.0, 4.1, 246.6, 17.5],
    ["Num", 1, 1, 4, 4],
    ["Data[%]", 25.0, 25.0, 100.0, 100.0],
]


def make_ods(sheets, engine="odf") -> bytes:
    """Build a real spreadsheet. `sheets` maps sheet name to a list of rows."""
    buffer = io.BytesIO()
    with pd.ExcelWriter(buffer, engine=engine) as writer:
        for name, rows in sheets.items():
            pd.DataFrame(rows).to_excel(writer, sheet_name=name, header=False, index=False)
    return buffer.getvalue()


def one_sheet(*rows, name="Sheet1") -> bytes:
    return make_ods({name: list(rows)})


def wind_frame(*readings):
    """Readings as (local time text, speed, direction)."""
    stamps = pd.to_datetime([r[0] for r in readings]).tz_localize("Africa/Johannesburg")
    return pd.DataFrame({"wind_timestamp": stamps, "wind_speed": [r[1] for r in readings],
                         "wind_direction": [r[2] for r in readings]})


def requests(*created):
    return pd.DataFrame({"notification_number": [str(i) for i in range(len(created))],
                         "creation_timestamp": list(created)})


# validate_spreadsheet

def test_a_real_ods_file_is_accepted():
    validate_spreadsheet(one_sheet(HEADER))


def test_a_real_xlsx_file_is_accepted():
    validate_spreadsheet(make_ods({"Sheet1": [HEADER]}, engine="openpyxl"))


def zip_bytes(members, compression=zipfile.ZIP_STORED) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression) as zf:
        for name, data in members.items():
            zf.writestr(name, data)
    return buffer.getvalue()


@pytest.mark.parametrize("payload", [
    b"<html>404 Not Found</html>",
    b"",
    b"PK this is not really a zip file",
    zip_bytes({"content.xml": b"<x/>"}),
    zip_bytes({"mimetype": b"application/zip"}),
])
def test_things_that_are_not_spreadsheets_raise_value_error(payload):
    with pytest.raises(ValueError):
        validate_spreadsheet(payload)


def test_truncated_spreadsheet_raises_value_error():
    payload = one_sheet(HEADER, ["2020-01-01 00:00", 1.0, 90.0])
    with pytest.raises(ValueError):
        validate_spreadsheet(payload[: len(payload) // 2])


@pytest.mark.parametrize("compression", [zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED])
def test_corrupt_member_raises_value_error(compression):
    content = bytes(range(256)) * 200
    payload = bytearray(zip_bytes({"mimetype": ODS_MIMETYPE, "content.xml": content}, compression))
    # Damage the middle of the member data, leaving the zip directory readable.
    middle = len(payload) // 2
    payload[middle] ^= 0xFF
    payload[middle + 1] ^= 0xFF
    with pytest.raises(ValueError):
        validate_spreadsheet(bytes(payload))


# The real layout

@pytest.mark.parametrize("engine", ["odf", "openpyxl"])
def test_real_layout_gives_atlantis_readings_only(engine):
    wind = parse_wind(make_ods({"Sheet1": REAL_LAYOUT}, engine=engine))
    assert wind.wind_speed[0] == 4.1
    assert wind.wind_direction[0] == 173.0
    assert 19.2 not in set(wind.wind_speed)


def test_real_layout_status_words_become_null_and_summary_rows_are_dropped():
    wind = parse_wind(make_ods({"Sheet1": REAL_LAYOUT}))
    assert len(wind) == 4
    assert wind.wind_speed.notna().sum() == 1
    assert wind.wind_direction.notna().sum() == 1


def test_real_layout_dates_are_day_first():
    wind = parse_wind(make_ods({"Sheet1": REAL_LAYOUT}))
    assert list(wind.wind_timestamp.dt.strftime("%Y-%m-%d %H:%M")) == [
        "2020-02-01 00:00", "2020-02-01 01:00", "2020-02-13 02:00", "2020-02-13 03:00"]


def test_a_site_name_in_a_merged_cell_reaches_both_its_columns():
    wind = parse_wind(one_sheet(
        ["Date & Time", "Goodwood", None, "Atlantis", None],
        [None, "Wind Dir", "Wind Speed", "Wind Dir", "Wind Speed"],
        ["01/02/2020 00:00", 90.0, 9.0, 10.0, 1.0],
    ))
    assert wind.wind_speed[0] == 1.0
    assert wind.wind_direction[0] == 10.0


# parse_wind

def test_parses_a_simple_sheet_as_local_time():
    wind = parse_wind(one_sheet(HEADER, ["2020-01-01 12:00", 3.5, 180.0]))
    assert list(wind.columns) == ["wind_timestamp", "wind_speed", "wind_direction"]
    assert wind.wind_timestamp[0] == pd.Timestamp("2020-01-01 10:00", tz="UTC")
    assert wind.wind_speed[0] == 3.5
    assert wind.wind_direction[0] == 180.0


def test_reads_real_date_cells_as_well_as_text():
    wind = parse_wind(one_sheet(HEADER, [pd.Timestamp("2020-03-01 06:30"), 1.0, 10.0]))
    assert wind.wind_timestamp[0] == pd.Timestamp("2020-03-01 06:30", tz="Africa/Johannesburg")


def test_finds_the_header_below_title_rows():
    wind = parse_wind(one_sheet(["Air quality report", None, None], [None, None, None], HEADER,
                                ["2020-01-01 12:00", 1.0, 2.0]))
    assert len(wind) == 1


def test_column_order_does_not_matter():
    wind = parse_wind(one_sheet(["Wind Direction", "Date", "Wind Speed"], [270.0, "2020-01-01 12:00", 4.0]))
    assert wind.wind_speed[0] == 4.0
    assert wind.wind_direction[0] == 270.0


def test_separate_date_and_time_columns_are_combined():
    wind = parse_wind(one_sheet(["Date", "Time", "Speed", "Direction"], ["2020-01-02", "13:30:00", 1.0, 2.0]))
    assert wind.wind_timestamp[0] == pd.Timestamp("2020-01-02 13:30", tz="Africa/Johannesburg")


@pytest.mark.parametrize("label", ["Date", "Date/Time", "DateTime", "Time", "Timestamp", "Date & Time"])
def test_timestamp_header_variants_are_recognised(label):
    wind = parse_wind(one_sheet([label, "Speed", "Direction"], ["2020-01-01 12:00", 1.0, 2.0]))
    assert len(wind) == 1


def test_day_first_text_dates_are_read_as_south_african_dates():
    wind = parse_wind(one_sheet(HEADER, ["01/02/2020 12:00", 1.0, 2.0], ["13/02/2020 12:00", 3.0, 4.0]))
    assert list(wind.wind_timestamp.dt.strftime("%Y-%m-%d")) == ["2020-02-01", "2020-02-13"]


def test_iso_text_dates_are_not_swapped_by_the_day_first_rule():
    wind = parse_wind(one_sheet(HEADER, ["2020-01-02 13:30", 1.0, 2.0]))
    assert wind.wind_timestamp[0] == pd.Timestamp("2020-01-02 13:30", tz="Africa/Johannesburg")


def test_mixed_date_styles_and_precision_all_parse_correctly():
    wind = parse_wind(one_sheet(HEADER, ["2020-01-02 13:30", 1.0, 1.0], ["2020-01-03 13:30:15", 2.0, 2.0],
                                ["04/01/2020 13:30", 3.0, 3.0]))
    assert list(wind.wind_timestamp.dt.strftime("%Y-%m-%d %H:%M:%S")) == [
        "2020-01-02 13:30:00", "2020-01-03 13:30:15", "2020-01-04 13:30:00"]


def test_a_timestamp_carrying_its_own_zone_is_not_treated_as_local():
    wind = parse_wind(one_sheet(HEADER, ["2020-01-01T10:00:00Z", 1.0, 2.0]))
    assert wind.wind_timestamp[0] == pd.Timestamp("2020-01-01 10:00", tz="UTC")


def test_sheets_covering_different_periods_are_combined():
    wind = parse_wind(make_ods({
        "January": [HEADER, ["2020-01-15 00:00", 1.0, 1.0]],
        "February": [HEADER, ["2020-02-15 00:00", 2.0, 2.0]],
    }))
    assert list(wind.wind_speed) == [1.0, 2.0]


def test_readings_are_sorted_and_unparseable_timestamps_dropped():
    wind = parse_wind(one_sheet(HEADER, ["2020-01-02 00:00", 2.0, 2.0], ["not a date", 9.0, 9.0],
                                ["2020-01-01 00:00", 1.0, 1.0]))
    assert list(wind.wind_speed) == [1.0, 2.0]


def test_duplicate_timestamps_keep_the_first():
    wind = parse_wind(one_sheet(HEADER, ["2020-01-01 00:00", 1.0, 1.0], ["2020-01-01 00:00", 9.0, 9.0]))
    assert list(wind.wind_speed) == [1.0]


def test_non_numeric_readings_become_null_but_keep_the_row():
    wind = parse_wind(one_sheet(HEADER, ["2020-01-01 00:00", "NoData", "Offline"]))
    assert len(wind) == 1
    assert wind.wind_speed.isna().all() and wind.wind_direction.isna().all()


def test_no_wind_sheet_raises_value_error():
    with pytest.raises(ValueError):
        parse_wind(one_sheet(["Name", "Value"], ["a", 1]))


def test_no_parseable_timestamps_raises_value_error():
    with pytest.raises(ValueError):
        parse_wind(one_sheet(HEADER, ["never", 1.0, 2.0]))


# The brief asks for the Atlantis site. A file covering several sites must never silently
# give another site's wind.

def test_the_atlantis_sheet_is_chosen_from_several_sites():
    wind = parse_wind(make_ods({
        "Goodwood": [HEADER, ["2020-01-01 00:00", 9.0, 9.0]],
        "Atlantis": [HEADER, ["2020-01-01 00:00", 1.0, 1.0]],
    }))
    assert list(wind.wind_speed) == [1.0]


def test_several_sites_with_no_atlantis_sheet_raises_instead_of_guessing():
    with pytest.raises(ValueError, match="Atlantis"):
        parse_wind(make_ods({
            "Goodwood": [HEADER, ["2020-01-01 00:00", 9.0, 9.0]],
            "Bellville": [HEADER, ["2020-01-01 00:00", 1.0, 1.0]],
        }))


def test_the_atlantis_columns_are_chosen_from_several_sites_on_one_sheet():
    wind = parse_wind(one_sheet(
        ["Date", "Goodwood Speed", "Goodwood Direction", "Atlantis Speed", "Atlantis Direction"],
        ["2020-01-01 00:00", 9.0, 90.0, 1.0, 10.0],
    ))
    assert wind.wind_speed[0] == 1.0
    assert wind.wind_direction[0] == 10.0


def test_several_unnamed_speed_columns_raises_instead_of_guessing():
    with pytest.raises(ValueError, match="Atlantis"):
        parse_wind(one_sheet(["Date", "Speed", "Direction", "Speed", "Direction"],
                             ["2020-01-01 00:00", 9.0, 90.0, 1.0, 10.0]))


# load_wind

def test_load_wind_downloads_validates_parses_and_caches(tmp_path):
    payload = one_sheet(HEADER, ["2020-01-01 00:00", 1.0, 2.0])
    wind = load_wind(tmp_path, urls=["https://x.example/w"], opener=lambda url, timeout: payload)
    assert len(wind) == 1
    assert (tmp_path / CACHE_FILE).read_bytes() == payload


def test_load_wind_falls_back_to_the_cache_when_the_download_fails(tmp_path):
    (tmp_path / CACHE_FILE).write_bytes(one_sheet(HEADER, ["2020-01-01 00:00", 1.0, 2.0]))

    def down(url, timeout):
        raise OSError("down")

    wind = load_wind(tmp_path, urls=["https://x.example/w"], opener=down, retries=1)
    assert len(wind) == 1


# attach_wind

WIND = wind_frame(("2020-01-01 12:00", 1.0, 10.0), ("2020-01-01 13:00", 2.0, 20.0), ("2020-01-01 18:00", 3.0, 30.0))


def test_the_nearest_reading_is_attached_from_either_side():
    out = attach_wind(requests("2020-01-01 12:10:00+02:00", "2020-01-01 12:50:00+02:00"), WIND)
    assert list(out.wind_speed) == [1.0, 2.0]


def test_time_zones_are_aligned_rather_than_compared_as_text():
    # 10:00 UTC is 12:00 in Cape Town.
    out = attach_wind(requests("2020-01-01 10:00:00+00:00"), WIND)
    assert out.wind_speed[0] == 1.0


def test_a_blank_reading_is_skipped_for_the_next_usable_one():
    wind = wind_frame(("2020-01-01 12:00", np.nan, np.nan), ("2020-01-01 12:50", 2.0, 20.0))
    out = attach_wind(requests("2020-01-01 12:05:00+02:00"), wind)
    assert out.wind_speed[0] == 2.0


def test_a_blank_reading_alone_gives_nulls():
    wind = wind_frame(("2020-01-01 12:00", np.nan, np.nan), ("2020-01-02 12:00", 2.0, 20.0))
    assert attach_wind(requests("2020-01-01 12:05:00+02:00"), wind).wind_speed.isna().all()


def test_a_reading_with_only_one_value_is_still_used():
    wind = wind_frame(("2020-01-01 12:00", 3.0, np.nan))
    out = attach_wind(requests("2020-01-01 12:05:00+02:00"), wind)
    assert out.wind_speed[0] == 3.0
    assert np.isnan(out.wind_direction[0])


def test_a_request_outside_the_tolerance_gets_nulls():
    out = attach_wind(requests("2020-01-01 15:30:00+02:00"), WIND)
    assert out.wind_speed.isna().all() and out.wind_direction.isna().all() and out.wind_timestamp.isna().all()


def test_tolerance_is_inclusive_and_configurable():
    created = requests("2020-01-01 14:00:00+02:00")
    assert attach_wind(created, WIND, tolerance=pd.Timedelta(hours=1)).wind_speed[0] == 2.0
    assert np.isnan(attach_wind(created, WIND, tolerance=pd.Timedelta(minutes=59)).wind_speed[0])


def test_original_row_order_and_index_are_kept():
    sr = requests("2020-01-01 18:00:00+02:00", "2020-01-01 12:00:00+02:00", "2020-01-01 13:00:00+02:00")
    sr.index = [50, 7, 21]
    out = attach_wind(sr, WIND)
    assert list(out.index) == [50, 7, 21]
    assert list(out.notification_number) == ["0", "1", "2"]
    assert list(out.wind_speed) == [3.0, 1.0, 2.0]


def test_unparseable_creation_time_gets_nulls_without_shifting_other_rows():
    out = attach_wind(requests("garbage", "2020-01-01 13:00:00+02:00", ""), WIND)
    assert len(out) == 3
    assert np.isnan(out.wind_speed[0]) and np.isnan(out.wind_speed[2])
    assert out.wind_speed[1] == 2.0


def test_the_input_is_not_changed():
    sr = requests("2020-01-01 12:00:00+02:00")
    before = sr.copy()
    attach_wind(sr, WIND)
    pd.testing.assert_frame_equal(sr, before)


def test_empty_requests_are_handled():
    out = attach_wind(requests(), WIND)
    assert len(out) == 0
    assert {"wind_timestamp", "wind_speed", "wind_direction"} <= set(out.columns)


@pytest.mark.network
@pytest.mark.skipif(not os.environ.get("RUN_NETWORK"), reason="set RUN_NETWORK=1 to hit the City portal")
def test_live_file_matches_the_summary_rows_printed_in_the_file_itself(tmp_path):
    wind = load_wind(tmp_path)
    assert len(wind) == 8784  # Hourly for all of 2020, a leap year.
    # Atlantis figures from the file's own Num, Minimum, Maximum and Avg rows.
    assert wind.wind_speed.notna().sum() == 2324
    assert wind.wind_direction.notna().sum() == 2324
    assert wind.wind_speed.min() == 0.4
    assert wind.wind_speed.max() == 11.2
    assert wind.wind_direction.min() == 0.1
    assert wind.wind_direction.max() == 358.0
    assert wind.wind_speed.mean() == pytest.approx(4.0, abs=0.1)
    assert wind.wind_timestamp.min() == pd.Timestamp("2020-01-01 00:00", tz="Africa/Johannesburg")
    assert wind.wind_timestamp.max() == pd.Timestamp("2020-12-31 23:00", tz="Africa/Johannesburg")
