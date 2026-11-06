import json
import os

import numpy as np
import pandas as pd
import pytest
from pyproj import Geod
from shapely.geometry import Point, shape

import atlantis_subsample
from atlantis_subsample import (
    ARC_MINUTE_M,
    SUBURB_NAME,
    SUBURB_NAME_FIELD,
    compute_centroid,
    distance_m,
    get_atlantis_centroid,
    subsample,
    suburb_query_url,
    validate_suburb_geojson,
)

CENTRE = (-33.5713, 18.4854)
GEOD = Geod(ellps="WGS84")


def point_at(metres, bearing=0.0, centre=CENTRE):
    """(lat, lon) of the point `metres` from centre, worked out with the forward geodesic."""
    lon, lat, _ = GEOD.fwd(centre[1], centre[0], bearing, metres)
    return lat, lon


def square(lat, lon, half):
    return [[lon - half, lat - half], [lon + half, lat - half], [lon + half, lat + half],
            [lon - half, lat + half], [lon - half, lat - half]]


def polygon(*rings):
    return {"type": "Polygon", "coordinates": list(rings)}


def frame(points):
    return pd.DataFrame({"notification_number": [str(i) for i in range(len(points))],
                         "latitude": [p[0] for p in points], "longitude": [p[1] for p in points]})


# Centroid

def test_centroid_of_a_symmetric_polygon_is_its_centre():
    lat, lon = compute_centroid(polygon(square(*CENTRE, 0.01)))
    assert lat == pytest.approx(CENTRE[0], abs=1e-5)
    assert lon == pytest.approx(CENTRE[1], abs=1e-5)


def test_centroid_is_area_weighted_not_a_mean_of_vertices():
    # Extra vertices along the east edge pull a vertex mean east, but do not change the area.
    lat, lon = -33.5, 18.5
    half = 0.01
    ring = [[lon - half, lat - half], [lon + half, lat - half], [lon + half, lat - 0.005],
            [lon + half, lat], [lon + half, lat + 0.005], [lon + half, lat + half],
            [lon - half, lat + half], [lon - half, lat - half]]
    _, centroid_lon = compute_centroid(polygon(ring))
    assert centroid_lon == pytest.approx(lon, abs=1e-5)


def test_centroid_of_a_multipolygon_is_pulled_towards_the_larger_part():
    big, small = square(-33.5, 18.4, 0.03), square(-33.5, 18.6, 0.01)
    _, lon = compute_centroid({"type": "MultiPolygon", "coordinates": [[big], [small]]})
    # Areas are 9:1, so the centroid sits 90% of the way towards the big square.
    assert lon == pytest.approx(18.42, abs=1e-3)


def test_centroid_ignores_the_area_of_a_hole():
    outer = square(-33.5, 18.5, 0.03)
    hole = square(-33.5, 18.52, 0.01)
    _, lon = compute_centroid(polygon(outer, hole))
    assert lon < 18.5


def test_self_intersecting_polygon_is_repaired_instead_of_crashing():
    bowtie = [[18.40, -33.60], [18.50, -33.50], [18.50, -33.60], [18.40, -33.50], [18.40, -33.60]]
    lat, lon = compute_centroid(polygon(bowtie))
    assert -33.60 <= lat <= -33.50
    assert 18.40 <= lon <= 18.50


# Distance

def test_distance_is_zero_at_the_centre():
    assert distance_m(pd.Series([CENTRE[0]]), pd.Series([CENTRE[1]]), CENTRE)[0] == pytest.approx(0, abs=1e-6)


@pytest.mark.parametrize("bearing", [0, 45, 90, 135, 180, 270])
def test_distance_matches_a_known_geodesic_in_every_direction(bearing):
    lat, lon = point_at(1500, bearing)
    assert distance_m(pd.Series([lat]), pd.Series([lon]), CENTRE)[0] == pytest.approx(1500, abs=0.01)


def test_a_minute_of_longitude_is_shorter_than_a_minute_of_latitude_here():
    north = distance_m(pd.Series([CENTRE[0] + 1 / 60]), pd.Series([CENTRE[1]]), CENTRE)[0]
    east = distance_m(pd.Series([CENTRE[0]]), pd.Series([CENTRE[1] + 1 / 60]), CENTRE)[0]
    assert north == pytest.approx(1848, abs=5)
    assert east == pytest.approx(1546, abs=5)


def test_distance_accepts_the_text_columns_the_csv_is_read_as():
    assert distance_m(pd.Series([str(CENTRE[0])]), pd.Series([str(CENTRE[1])]), CENTRE)[0] == pytest.approx(0, abs=1e-6)


@pytest.mark.parametrize("lat, lon", [("", ""), ("abc", "18.4"), ("-33.5", ""), ("95", "18.4"),
                                      ("-33.5", "200"), ("inf", "18.4")])
def test_unusable_coordinates_give_nan(lat, lon):
    assert np.isnan(distance_m(pd.Series([lat]), pd.Series([lon]), CENTRE)[0])


# Subsample

def test_subsample_keeps_only_points_inside_the_radius():
    near, far = point_at(1800), point_at(1900)
    out = subsample(frame([near, far]), CENTRE)
    assert list(out["notification_number"]) == ["0"]


def test_subsample_uses_a_circle_not_a_lat_lon_box():
    # Inside a 1 minute box (only 1 minute east and 1 minute north) but outside the circle.
    corner = (CENTRE[0] + 0.9 / 60, CENTRE[1] + 0.9 / 60)
    assert len(subsample(frame([corner]), CENTRE)) == 0


def test_subsample_radius_is_inclusive_at_the_boundary():
    lat, lon = point_at(1000)
    exact = float(distance_m(pd.Series([lat]), pd.Series([lon]), CENTRE)[0])
    assert len(subsample(frame([(lat, lon)]), CENTRE, radius_m=exact)) == 1
    assert len(subsample(frame([(lat, lon)]), CENTRE, radius_m=exact - 0.001)) == 0


def test_default_radius_is_one_arc_minute():
    assert ARC_MINUTE_M == 1852.0


def test_subsample_excludes_rows_without_coordinates():
    df = pd.DataFrame({"latitude": ["", str(CENTRE[0])], "longitude": ["", str(CENTRE[1])]})
    assert len(subsample(df, CENTRE)) == 1


def test_subsample_adds_rounded_distance_and_keeps_columns_and_values():
    lat, lon = point_at(1234.56)
    df = frame([(lat, lon)])
    df["code"] = ["x"]
    out = subsample(df, CENTRE)
    assert list(out.columns) == ["notification_number", "latitude", "longitude", "code", "distance_to_centroid_m"]
    assert out["distance_to_centroid_m"].iloc[0] == pytest.approx(1234.6)
    assert out["code"].iloc[0] == "x"


def test_subsample_does_not_change_its_input():
    df = frame([CENTRE])
    before = df.copy()
    subsample(df, CENTRE)
    pd.testing.assert_frame_equal(df, before)


def test_subsample_handles_an_empty_frame():
    assert len(subsample(frame([]), CENTRE)) == 0


# Suburb download

def collection(*features):
    return json.dumps({"type": "FeatureCollection", "features": list(features)}).encode()


FEATURE = {"type": "Feature", "properties": {SUBURB_NAME_FIELD: SUBURB_NAME},
           "geometry": polygon(square(*CENTRE, 0.01))}
NAMED = {"type": "Feature", "properties": {SUBURB_NAME_FIELD: SUBURB_NAME}}


def test_the_suburb_is_the_official_one_used_by_the_service_request_data():
    assert SUBURB_NAME == "ATLANTIS INDUSTRIAL"
    assert SUBURB_NAME_FIELD == "OFC_SBRB_N"
    assert "/MapServer/23/query" in suburb_query_url()


def test_query_url_asks_for_the_named_suburb_as_wgs84_geojson():
    url = suburb_query_url("atlantis industrial")
    assert "ATLANTIS+INDUSTRIAL" in url
    assert "outSR=4326" in url
    assert "f=geojson" in url
    assert "returnGeometry=true" in url


def test_valid_suburb_payload_passes():
    validate_suburb_geojson(collection(FEATURE))


@pytest.mark.parametrize("payload", [
    b"<html>error page</html>",
    b"\xff\xfe not text",
    b"[]",
    b"null",
    b"42",
    json.dumps({"error": {"code": 500}}).encode(),
    collection(),
    collection(FEATURE, FEATURE),
    collection({**NAMED, "geometry": None}),
    collection(NAMED),
    collection({**NAMED, "geometry": {"type": "Point", "coordinates": [18.4, -33.5]}}),
    collection({**NAMED, "properties": None, "geometry": polygon(square(*CENTRE, 0.01))}),
    collection({**NAMED, "properties": {SUBURB_NAME_FIELD: "ATLANTIS"}, "geometry": polygon(square(*CENTRE, 0.01))}),
    collection({**NAMED, "properties": {"SBRB_NAME": "ATLANTIS"}, "geometry": polygon(square(*CENTRE, 0.01))}),
    collection("not a feature"),
])
def test_bad_suburb_payloads_raise_value_error_so_the_fetch_retries(payload):
    with pytest.raises(ValueError):
        validate_suburb_geojson(payload)


def test_server_error_message_is_reported_rather_than_a_generic_count():
    with pytest.raises(ValueError, match="server error"):
        validate_suburb_geojson(json.dumps({"error": {"code": 500}}).encode())


def test_get_atlantis_centroid_uses_the_download_and_the_cache_path(monkeypatch, tmp_path):
    seen = {}

    def fake_fetch(urls, cache_path, validate):
        seen.update(urls=urls, cache_path=cache_path, validate=validate)
        return collection(FEATURE), "cache"

    monkeypatch.setattr(atlantis_subsample, "fetch_bytes", fake_fetch)
    lat, lon = get_atlantis_centroid(tmp_path)
    assert lat == pytest.approx(CENTRE[0], abs=1e-5)
    assert lon == pytest.approx(CENTRE[1], abs=1e-5)
    assert seen["urls"] == [suburb_query_url()]
    assert seen["cache_path"] == tmp_path / "atlantis_suburb.geojson"
    assert seen["validate"] is validate_suburb_geojson


@pytest.mark.network
@pytest.mark.skipif(not os.environ.get("RUN_NETWORK"), reason="set RUN_NETWORK=1 to hit the GIS server")
def test_live_atlantis_centroid_is_inside_the_real_polygon(tmp_path):
    lat, lon = get_atlantis_centroid(tmp_path)
    payload = json.loads((tmp_path / "atlantis_suburb.geojson").read_bytes())
    assert shape(payload["features"][0]["geometry"]).contains(Point(lon, lat))
