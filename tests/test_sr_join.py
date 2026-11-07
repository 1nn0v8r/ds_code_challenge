import h3
import pandas as pd
import pytest

from sr_join import (
    INDEX_COLUMN,
    JoinStats,
    JoinThresholdError,
    assign_hex_index,
    compare_to_reference,
    enforce_threshold,
    find_column,
    join_service_requests,
)

CBD = (-33.9249, 18.4241)
CBD_CELL = h3.latlng_to_cell(*CBD, 8)
NEARBY = (-33.9300, 18.4300)
NEARBY_CELL = h3.latlng_to_cell(*NEARBY, 8)
JOBURG = (-26.2041, 28.0473)


@pytest.fixture
def hexes():
    return {CBD_CELL, NEARBY_CELL}


def run(rows, hexes):
    df = pd.DataFrame(rows, columns=["latitude", "longitude"])
    return assign_hex_index(df.latitude, df.longitude, hexes)


def test_valid_coordinates_get_their_cell(hexes):
    index, stats = run([CBD, NEARBY], hexes)
    assert list(index) == [CBD_CELL, NEARBY_CELL]
    assert stats.failed == 0 and stats.joined == 2


def test_missing_coordinates_get_zero_and_are_not_failures(hexes):
    index, stats = run([(None, None), CBD], hexes)
    assert list(index) == ["0", CBD_CELL]
    assert stats.missing_coords == 1
    assert stats.failed == 0
    assert stats.error_rate == 0.0


def test_partial_coordinates_are_treated_as_missing_and_counted(hexes):
    index, stats = run([(CBD[0], None), (None, CBD[1])], hexes)
    assert list(index) == ["0", "0"]
    assert stats.missing_coords == 2 and stats.partial_coords == 2
    assert stats.failed == 0


def test_cell_outside_grid_is_a_failure_with_zero_index(hexes):
    index, stats = run([JOBURG, CBD], hexes)
    assert list(index) == ["0", CBD_CELL]
    assert stats.outside_grid == 1 and stats.failed == 1
    assert stats.error_rate == pytest.approx(0.5)


def test_invalid_range_is_counted_as_invalid_not_crashing(hexes):
    index, stats = run([(95.0, 18.0), (-33.9, 400.0)], hexes)
    assert list(index) == ["0", "0"]
    assert stats.invalid_coords == 2 and stats.failed == 2


def test_non_numeric_coordinates_are_treated_as_missing(hexes):
    df = pd.DataFrame({"latitude": ["abc", str(CBD[0])], "longitude": ["x", str(CBD[1])]})
    index, stats = assign_hex_index(df.latitude, df.longitude, hexes)
    assert list(index) == ["0", CBD_CELL]
    assert stats.missing_coords == 1


def test_index_is_string_so_zero_survives_csv_round_trip(hexes, tmp_path):
    index, _ = run([(None, None), CBD], hexes)
    path = tmp_path / "x.csv"
    pd.DataFrame({INDEX_COLUMN: index}).to_csv(path, index=False)
    back = pd.read_csv(path, dtype=str)[INDEX_COLUMN]
    assert list(back) == ["0", CBD_CELL]


def test_threshold_raises_above_and_passes_at_or_below():
    over = JoinStats(
        total=1000, missing_coords=0, partial_coords=0, invalid_coords=0, outside_grid=2
    )
    with pytest.raises(JoinThresholdError):
        enforce_threshold(over, 0.001)
    enforce_threshold(over, 0.002)


def test_threshold_denominator_is_located_rows_only():
    stats = JoinStats(
        total=1000, missing_coords=900, partial_coords=0, invalid_coords=0, outside_grid=1
    )
    assert stats.located == 100
    assert stats.error_rate == pytest.approx(0.01)


def test_threshold_with_no_located_rows_does_not_divide_by_zero():
    stats = JoinStats(total=5, missing_coords=5, partial_coords=0, invalid_coords=0, outside_grid=0)
    enforce_threshold(stats, 0.001)


def test_join_service_requests_preserves_rows_and_adds_column(hexes):
    sr = pd.DataFrame(
        {
            "notification_number": [1, 2, 3],
            "Latitude": [CBD[0], None, NEARBY[0]],
            "Longitude": [CBD[1], None, NEARBY[1]],
        }
    )
    out = join_service_requests(sr, hexes, 0.001)
    assert len(out) == 3
    assert list(out["notification_number"]) == [1, 2, 3]
    assert list(out[INDEX_COLUMN]) == [CBD_CELL, "0", NEARBY_CELL]
    assert INDEX_COLUMN not in sr.columns


def test_join_service_requests_raises_when_over_threshold(hexes):
    sr = pd.DataFrame({"latitude": [CBD[0], JOBURG[0]], "longitude": [CBD[1], JOBURG[1]]})
    with pytest.raises(JoinThresholdError):
        join_service_requests(sr, hexes, 0.001)


def test_join_drops_leaked_pandas_index_column_but_keeps_real_columns(hexes):
    sr = pd.DataFrame(
        {
            "Unnamed: 0": [0],
            "notification_number": [7],
            "latitude": [CBD[0]],
            "longitude": [CBD[1]],
            "Unnamed: Not A Number": ["keep"],
        }
    )
    out = join_service_requests(sr, hexes, 0.001)
    assert list(out.columns) == [
        "notification_number",
        "latitude",
        "longitude",
        "Unnamed: Not A Number",
        INDEX_COLUMN,
    ]


def test_join_refuses_input_that_already_has_index_column(hexes):
    sr = pd.DataFrame({"latitude": [CBD[0]], "longitude": [CBD[1]], INDEX_COLUMN: ["x"]})
    with pytest.raises(ValueError):
        join_service_requests(sr, hexes, 0.001)


def test_find_column_is_case_insensitive():
    df = pd.DataFrame(columns=["Latitude", "longitude"])
    assert find_column(df, "latitude") == "Latitude"
    assert find_column(df, "Longitude") == "longitude"
    with pytest.raises(KeyError):
        find_column(df, "altitude")


def _frames(ours, theirs):
    a = pd.DataFrame({"notification_number": range(len(ours)), INDEX_COLUMN: ours})
    b = pd.DataFrame({"notification_number": range(len(theirs)), INDEX_COLUMN: theirs})
    return a, b


def test_compare_flags_cell_outside_grid_as_expected_only(hexes):
    ours, theirs = _frames([CBD_CELL, "0"], [CBD_CELL, "88ad000000fffff"])
    result = compare_to_reference(ours, theirs, hexes)
    assert result["expected_differences"] == [1]
    assert result["unexpected_differences"] == []
    assert result["matches"]


def test_compare_flags_real_disagreement_as_unexpected(hexes):
    ours, theirs = _frames([CBD_CELL, NEARBY_CELL], [CBD_CELL, CBD_CELL])
    result = compare_to_reference(ours, theirs, hexes)
    assert result["unexpected_differences"] == [1]
    assert not result["matches"]


def test_compare_flags_zero_vs_in_grid_cell_as_unexpected(hexes):
    ours, theirs = _frames(["0"], [CBD_CELL])
    assert not compare_to_reference(ours, theirs, hexes)["matches"]


def test_compare_flags_missing_rows(hexes):
    ours, theirs = _frames([CBD_CELL], [CBD_CELL, CBD_CELL])
    result = compare_to_reference(ours, theirs, hexes)
    assert result["only_in_reference"] == 1 and not result["matches"]


def test_compare_rejects_duplicate_keys(hexes):
    a = pd.DataFrame({"notification_number": [1, 1], INDEX_COLUMN: [CBD_CELL, CBD_CELL]})
    with pytest.raises(ValueError):
        compare_to_reference(a, a, hexes)
