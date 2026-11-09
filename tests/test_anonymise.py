import logging

import numpy as np
import pandas as pd
import pytest

from anonymise import (
    DROP_COLUMNS,
    WIND_DIRECTION_MEAN,
    WIND_SPEED_MEAN,
    anonymise,
    check_precision,
    floor_to_bucket,
    summarise_wind_by_bucket,
)

HEX_A = "88ad361801fffff"
HEX_B = "88ad361803fffff"


def record(number=0, **overrides):
    row = {
        "notification_number": f"{number:012d}",
        "reference_number": f"{number + 5000}",
        "creation_timestamp": "2020-03-10 08:15:30+02:00",
        "completion_timestamp": "2020-03-11 13:45:00+02:00",
        "directorate": "WATER AND SANITATION",
        "department": "Distribution",
        "branch": "Secret branch",
        "section": "Secret section",
        "code_group": "WATER",
        "code": "Burst Pipe",
        "cause_code_group": "",
        "cause_code": "",
        "official_suburb": "ROBINVALE",
        "latitude": "-33.5713",
        "longitude": "18.4854",
        "h3_level8_index": HEX_A,
        "distance_to_centroid_m": "123.4",
        "wind_timestamp": "2020-03-10 08:00:00+02:00",
        "wind_speed": "4.7",
        "wind_direction": "213.0",
    }
    row.update(overrides)
    return row


def frame(*rows):
    return pd.DataFrame(list(rows))


def common(n=10, start=0, **overrides):
    """Records that nothing should hold back."""
    return [record(start + i, **overrides) for i in range(n)]


def readings(*rows):
    """Wind readings as (local time text, speed, direction)."""
    stamps = pd.to_datetime([r[0] for r in rows]).tz_localize("Africa/Johannesburg")
    return pd.DataFrame(
        {
            "wind_timestamp": stamps,
            "wind_speed": [r[1] for r in rows],
            "wind_direction": [r[2] for r in rows],
        }
    )


# floor_to_bucket


@pytest.mark.parametrize(
    "value, expected",
    [
        ("2020-01-01 05:59:59+02:00", "2020-01-01 00:00"),
        ("2020-01-01 06:00:00+02:00", "2020-01-01 06:00"),
        ("2020-01-01 23:59:59+02:00", "2020-01-01 18:00"),
        # 03:59 UTC is 05:59 in Cape Town, so the bucket follows local time.
        ("2020-01-01 03:59:00+00:00", "2020-01-01 00:00"),
        ("2019-12-31 23:00:00+00:00", "2020-01-01 00:00"),
        ("", ""),
        ("garbage", ""),
    ],
)
def test_floor_to_bucket_uses_local_six_hour_buckets(value, expected):
    assert floor_to_bucket(pd.Series([value]))[0] == expected


def test_floor_to_bucket_keeps_mixed_timestamp_formats():
    # Whole seconds and fractional seconds in one column must both be bucketed,
    # not blanked because pandas guessed the format from the first value.
    values = pd.Series(["2020-01-01 05:59:59+02:00", "2020-01-01 06:00:00.500+02:00"])
    assert list(floor_to_bucket(values)) == ["2020-01-01 00:00", "2020-01-01 06:00"]


# What is removed or coarsened


def test_every_identifying_column_is_removed():
    out = anonymise(frame(*common())).anonymised
    assert not set(DROP_COLUMNS) & set(out.columns)


def test_official_suburb_is_removed_because_it_can_be_finer_than_the_hexagon():
    assert "official_suburb" not in anonymise(frame(*common())).anonymised.columns


def test_no_original_values_of_identifying_columns_survive_anywhere():
    out = anonymise(frame(*common())).anonymised
    text = out.astype(str).to_csv()
    for secret in ("-33.5713", "18.4854", "Secret branch", "ROBINVALE", "000000000001", "213.0"):
        assert secret not in text


def test_hexagon_and_analytical_columns_are_kept():
    out = anonymise(frame(*common())).anonymised
    assert set(out["h3_level8_index"]) == {HEX_A}
    assert set(out["code"]) == {"Burst Pipe"}


def test_timestamps_are_floored_to_six_hours():
    out = anonymise(frame(*common())).anonymised
    assert set(out["creation_timestamp"]) == {"2020-03-10 06:00"}
    assert set(out["completion_timestamp"]) == {"2020-03-11 12:00"}


def test_blank_completion_stays_blank():
    out = anonymise(frame(*common(completion_timestamp=""))).anonymised
    assert set(out["completion_timestamp"]) == {""}


# Surrogate ids


def test_record_ids_are_unique_and_unrelated_to_the_originals():
    df = frame(*common(50))
    out = anonymise(df, seed=1).anonymised
    assert out["record_id"].is_unique
    assert not set(out["record_id"]) & set(df["notification_number"])


def test_output_order_does_not_reveal_input_order():
    rows = [record(i, creation_timestamp=f"2020-03-{i + 1:02d} 08:00:00+02:00") for i in range(28)]
    out = anonymise(frame(*rows), seed=1).anonymised
    assert list(out["creation_timestamp"]) != sorted(out["creation_timestamp"])


def test_seed_reproduces_and_no_seed_does_not():
    # Sorted ids hide the shuffle, so compare which record got which id via a varying column.
    rows = [
        record(i, creation_timestamp=f"2020-03-{i % 28 + 1:02d} 08:00:00+02:00") for i in range(200)
    ]
    df = frame(*rows)
    seeded = [anonymise(df, seed=7).anonymised["creation_timestamp"].tolist() for _ in range(2)]
    assert seeded[0] == seeded[1]
    unseeded = [anonymise(df).anonymised["creation_timestamp"].tolist() for _ in range(2)]
    assert unseeded[0] != unseeded[1]


# What is held for review


def test_a_rare_code_is_held_at_the_threshold_boundary():
    rows = common(10) + common(4, start=100, code="Bees in Meter")
    result = anonymise(frame(*rows))
    assert set(result.held_for_review["code"]) == {"Bees in Meter"}
    assert set(result.held_for_review["review_reason"]) == {"RARE_VALUE"}


def test_a_code_seen_exactly_at_the_minimum_is_kept():
    rows = common(10) + common(5, start=100, code="Bees in Meter")
    assert len(anonymise(frame(*rows)).held_for_review) == 0


def test_a_rare_cause_code_is_held():
    rows = common(10) + common(1, start=100, cause_code="Vehicle Impact")
    held = anonymise(frame(*rows)).held_for_review
    assert list(held["cause_code"]) == ["Vehicle Impact"]


def test_blank_values_are_never_treated_as_rare():
    rows = common(10) + common(1, start=100, cause_code="", department="Distribution")
    assert len(anonymise(frame(*rows)).held_for_review) == 0


def test_a_few_blank_values_among_many_filled_ones_are_kept():
    rows = common(10, cause_code="Joint Failure") + common(1, start=100, cause_code="")
    assert len(anonymise(frame(*rows)).held_for_review) == 0


def test_holding_records_back_can_make_another_value_rare():
    # Five "Leak" rows, three of them with a one-off cause that gets them held. That leaves two
    # "Leak" rows, now rare themselves, so they must be held too.
    rows = common(10)
    rows += [record(100 + i, code="Leak", cause_code=f"Cause {i}") for i in range(3)]
    rows += [record(200 + i, code="Leak") for i in range(2)]
    result = anonymise(frame(*rows))
    assert "Leak" not in set(result.anonymised["code"])
    assert len(result.held_for_review) == 5


def sensitive(number, **overrides):
    return record(
        number, department="Revenue", code_group="REVENUE", code="Account Query", **overrides
    )


def test_small_sensitive_class_is_held():
    # Ten in one hexagon keep the code from being rare; the two elsewhere are a small class.
    rows = [sensitive(100 + i) for i in range(10)] + [
        sensitive(200 + i, h3_level8_index=HEX_B) for i in range(2)
    ]
    held = anonymise(frame(*rows)).held_for_review
    assert set(held["review_reason"]) == {"SENSITIVE_SMALL_CLASS"}
    assert set(held["h3_level8_index"]) == {HEX_B}


def test_sensitive_class_at_the_k_threshold_is_kept():
    rows = [sensitive(100 + i) for i in range(10)] + [
        sensitive(200 + i, h3_level8_index=HEX_B) for i in range(3)
    ]
    assert len(anonymise(frame(*rows)).held_for_review) == 0


def test_sensitive_class_is_split_by_six_hour_bucket():
    rows = [sensitive(100 + i) for i in range(10)]
    rows += [sensitive(200 + i, creation_timestamp="2020-03-10 14:00:00+02:00") for i in range(2)]
    held = anonymise(frame(*rows)).held_for_review
    assert list(held["notification_number"]) == ["000000000200", "000000000201"]


def test_small_class_outside_sensitive_areas_is_kept():
    rows = common(10) + common(
        5, start=100, h3_level8_index=HEX_B, code_group="ROADS", code="Pothole"
    )
    assert len(anonymise(frame(*rows)).held_for_review) == 0


def test_held_records_keep_original_values_for_the_reviewer():
    rows = common(10) + common(1, start=100, code="Bees in Meter")
    held = anonymise(frame(*rows)).held_for_review
    assert held["notification_number"].iloc[0] == "000000000100"
    assert held["latitude"].iloc[0] == "-33.5713"


def test_every_record_ends_up_in_exactly_one_output():
    rows = common(10) + common(2, start=100, code="Rare") + [sensitive(200)]
    result = anonymise(frame(*rows))
    assert len(result.anonymised) + len(result.held_for_review) == len(rows)
    assert result.summary["input_records"] == len(rows)
    assert result.summary["anonymised_records"] == len(result.anonymised)
    assert result.summary["held_for_review"] == len(result.held_for_review)
    assert result.summary["held_rare_value"] + result.summary["held_sensitive_small_class"] == len(
        result.held_for_review
    )


# Refusing bad input


def test_missing_required_column_raises():
    with pytest.raises(KeyError):
        anonymise(frame(*common()).drop(columns=["code"]))


@pytest.mark.parametrize("index", ["0", ""])
def test_rows_without_a_hexagon_are_refused(index):
    with pytest.raises(ValueError):
        anonymise(frame(*common(h3_level8_index=index)))


def test_unparseable_creation_time_is_refused():
    with pytest.raises(ValueError):
        anonymise(frame(*common(creation_timestamp="garbage")))


def test_the_input_is_not_changed():
    df = frame(*common())
    before = df.copy()
    anonymise(df)
    pd.testing.assert_frame_equal(df, before)


# Wind


def test_speed_is_the_mean_for_the_bucket():
    means = summarise_wind_by_bucket(
        readings(("2020-03-10 06:00", 2.0, 90.0), ("2020-03-10 11:00", 4.0, 90.0))
    )
    assert means.loc["2020-03-10 06:00", WIND_SPEED_MEAN] == 3.0


def test_direction_is_a_circular_mean():
    means = summarise_wind_by_bucket(
        readings(("2020-03-10 06:00", 1.0, 350.0), ("2020-03-10 07:00", 1.0, 10.0))
    )
    assert means.loc["2020-03-10 06:00", WIND_DIRECTION_MEAN] == 0.0


def test_direction_rounding_cannot_give_360():
    means = summarise_wind_by_bucket(readings(("2020-03-10 06:00", 1.0, 359.7)))
    assert means.loc["2020-03-10 06:00", WIND_DIRECTION_MEAN] == 0.0


def test_readings_in_other_buckets_do_not_count():
    means = summarise_wind_by_bucket(
        readings(
            ("2020-03-10 05:00", 100.0, 90.0),
            ("2020-03-10 06:00", 2.0, 90.0),
            ("2020-03-10 12:00", 100.0, 90.0),
        )
    )
    assert means.loc["2020-03-10 06:00", WIND_SPEED_MEAN] == 2.0


def test_missing_readings_are_ignored():
    means = summarise_wind_by_bucket(
        readings(("2020-03-10 06:00", np.nan, np.nan), ("2020-03-10 07:00", 2.0, 90.0))
    )
    assert means.loc["2020-03-10 06:00", WIND_SPEED_MEAN] == 2.0
    assert means.loc["2020-03-10 06:00", WIND_DIRECTION_MEAN] == 90.0


WIND = readings(
    ("2020-03-10 06:00", 2.0, 90.0),
    ("2020-03-10 08:00", 6.0, 90.0),
    ("2020-03-10 12:00", 9.0, 180.0),
)


def test_requests_in_a_bucket_all_get_the_same_values_whatever_they_matched():
    # Different exact readings per request, which is what would identify the time.
    rows = [
        record(i, wind_speed=str(float(i)), wind_direction=str(float(i * 30))) for i in range(10)
    ]
    out = anonymise(frame(*rows), wind=WIND).anonymised
    assert set(out[WIND_SPEED_MEAN]) == {4.0}
    assert set(out[WIND_DIRECTION_MEAN]) == {90.0}


def test_the_values_come_from_the_readings_not_the_requests():
    out = anonymise(frame(*common(wind_speed="99.0")), wind=WIND).anonymised
    assert set(out[WIND_SPEED_MEAN]) == {4.0}


def test_no_readings_in_a_bucket_gives_nulls():
    rows = common(creation_timestamp="2020-06-01 08:00:00+02:00")
    assert anonymise(frame(*rows), wind=WIND).anonymised[WIND_SPEED_MEAN].isna().all()


def test_without_readings_exact_wind_is_still_removed_with_a_warning(caplog):
    with caplog.at_level(logging.WARNING, logger="anonymise"):
        out = anonymise(frame(*common())).anonymised
    assert not {"wind_speed", "wind_direction", WIND_SPEED_MEAN} & set(out.columns)
    assert "exact wind columns removed" in caplog.text


def test_skipping_wind_entirely_gives_no_warning(caplog):
    rows = frame(*common()).drop(columns=["wind_timestamp", "wind_speed", "wind_direction"])
    with caplog.at_level(logging.WARNING, logger="anonymise"):
        anonymise(rows)
    assert caplog.text == ""


# check_precision


def clean_output():
    df = frame(*common())
    return df, anonymise(df, wind=WIND).anonymised


def test_a_clean_output_passes():
    df, out = clean_output()
    assert check_precision(df, out) == {"ok": True, "problems": []}


@pytest.mark.parametrize("column", DROP_COLUMNS)
def test_a_leaked_column_is_flagged(column):
    df, out = clean_output()
    out[column] = "leak"
    assert any(column in p for p in check_precision(df, out)["problems"])


def test_a_time_off_the_boundary_is_flagged():
    df, out = clean_output()
    out.loc[0, "creation_timestamp"] = "2020-03-10 07:00"
    assert any("creation_timestamp" in p for p in check_precision(df, out)["problems"])


def test_wind_varying_within_a_bucket_is_flagged():
    df, out = clean_output()
    out.loc[0, WIND_SPEED_MEAN] = 1.23
    assert any(WIND_SPEED_MEAN in p for p in check_precision(df, out)["problems"])


def test_duplicate_record_id_is_flagged():
    df, out = clean_output()
    out.loc[1, "record_id"] = out.loc[0, "record_id"]
    assert any("duplicate" in p for p in check_precision(df, out)["problems"])


def test_record_id_matching_an_original_is_flagged():
    df, out = clean_output()
    out.loc[0, "record_id"] = df.loc[0, "notification_number"]
    assert any("original" in p for p in check_precision(df, out)["problems"])
