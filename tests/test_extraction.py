import copy
import json
import os

import pytest

from aws_client import BUCKET, make_s3_client
from conformance import ConformanceError, h3_resolution, load_schema, score_collection, validate
from extract import QUERY_TEMPLATE, extract_hexes, parse_collection
from run_extraction import compare_to_reference, load_reference


def make_feature(index="88ad361801fffff", lat=-33.86, lon=18.68, resolution=8):
    ring = [
        [lon + d, lat + e]
        for d, e in [
            (0.003, 0.0),
            (0.005, 0.004),
            (0.002, 0.008),
            (-0.003, 0.008),
            (-0.005, 0.004),
            (-0.002, 0.0),
        ]
    ]
    ring.append(ring[0])
    return {
        "type": "Feature",
        "properties": {
            "index": index,
            "centroid_lat": lat + 0.004,
            "centroid_lon": lon,
            "resolution": resolution,
        },
        "geometry": {"type": "Polygon", "coordinates": [ring]},
    }


def make_collection(n=3800):
    features = []
    for i in range(n):
        features.append(
            make_feature(
                index=f"88ad{i:011x}", lat=-34.0 + (i % 100) * 0.005, lon=18.4 + (i // 100) * 0.01
            )
        )
    return {
        "type": "FeatureCollection",
        "crs": {"type": "name", "properties": {"name": "urn:ogc:def:crs:OGC:1.3:CRS84"}},
        "features": features,
    }


@pytest.fixture
def schema():
    return load_schema()


def test_h3_resolution_decodes_known_index():
    assert h3_resolution("88ad361801fffff") == 8


@pytest.mark.parametrize("bad", ["nothex", "", None, "ffffffffffffffff"])
def test_h3_resolution_rejects_garbage(bad):
    assert h3_resolution(bad) is None


def test_clean_collection_scores_perfectly(schema):
    report = score_collection(make_collection(), schema)
    assert report.score == pytest.approx(1.0)
    assert report.status == "PASS"


def test_score_is_non_binary_and_degrades_gradually(schema):
    scores = []
    for bad in (0, 20, 200, 1000):
        coll = make_collection()
        for feat in coll["features"][:bad]:
            feat["properties"]["resolution"] = 9
        scores.append(score_collection(coll, schema).score)
    assert scores == sorted(scores, reverse=True)
    assert len(set(scores)) == len(scores)


def test_single_bad_feature_does_not_fail_dataset(schema):
    coll = make_collection()
    coll["features"][0]["geometry"] = None
    report = score_collection(coll, schema)
    assert report.status == "PASS"
    assert report.score < 1.0


def test_wholesale_garbage_fails_and_validate_raises(schema):
    coll = make_collection()
    for feat in coll["features"]:
        feat["properties"] = {}
        feat["geometry"] = None
    coll["type"] = "Nope"
    with pytest.raises(ConformanceError):
        validate(coll, schema)


def test_duplicate_indexes_are_penalised(schema):
    coll = make_collection()
    coll["features"][1]["properties"]["index"] = coll["features"][0]["properties"]["index"]
    report = score_collection(coll, schema)
    assert report.rule_pass_rates["unique_property"] == 0.0
    assert report.score == pytest.approx(0.9143, abs=1e-3)
    assert report.status == "FAIL"


def test_empty_collection_fails(schema):
    report = score_collection({"type": "FeatureCollection", "features": []}, schema)
    assert report.status == "FAIL"


def test_parse_collection_rebuilds_header_and_features():
    f = make_feature()
    lines = [
        '"type": "FeatureCollection",',
        '"crs": { "type": "name", "properties": { "name": "urn:ogc:def:crs:OGC:1.3:CRS84" } },',
        json.dumps(f).replace('{"type": "Feature"', '{ "type": "Feature"', 1) + ",",
    ]
    parsed = parse_collection(lines)
    assert parsed["type"] == "FeatureCollection"
    assert parsed["crs"]["properties"]["name"].endswith("CRS84")
    assert parsed["features"] == [f]


def test_query_template_filters_on_requested_resolution():
    assert '"resolution": 8 }' in QUERY_TEMPLATE.format(res=8)
    assert '"resolution": 9 }' in QUERY_TEMPLATE.format(res=9)


def test_compare_to_reference_detects_differences():
    ref = make_collection(5)
    ext = copy.deepcopy(ref)
    assert compare_to_reference(ext, ref)["matches"]

    ext["features"].pop()
    ext["features"][0]["properties"]["centroid_lat"] += 1
    result = compare_to_reference(ext, ref)
    assert not result["matches"]
    assert len(result["missing"]) == 1
    assert len(result["geometry_mismatch"]) == 1


@pytest.mark.network
@pytest.mark.skipif(not os.environ.get("RUN_NETWORK"), reason="set RUN_NETWORK=1 to hit S3")
def test_live_extract_matches_reference():
    s3 = make_s3_client()
    extracted = extract_hexes(s3, BUCKET)
    assert compare_to_reference(extracted, load_reference(s3))["matches"]
    assert validate(extracted).status == "PASS"


def test_a_feature_that_is_not_an_object_scores_zero_but_does_not_crash(schema):
    coll = make_collection()
    coll["features"][0] = "not a feature"
    report = score_collection(coll, schema)
    assert report.rule_pass_rates["is_feature"] < 1.0


def test_missing_centroid_or_ring_fails_those_rules_but_does_not_crash(schema):
    coll = make_collection()
    del coll["features"][0]["properties"]["centroid_lat"]
    coll["features"][1]["geometry"]["coordinates"] = [[]]
    report = score_collection(coll, schema)
    assert report.rule_pass_rates["centroid_inside_polygon_bbox"] < 1.0


@pytest.mark.parametrize("section", ["feature_rules", "dataset_rules"])
def test_a_misspelt_rule_in_the_schema_raises_rather_than_being_ignored(schema, section):
    schema[section].append({"check": "no_such_rule", "weight": 1})
    with pytest.raises(ValueError, match="unknown"):
        score_collection(make_collection(5), schema)
