"""Schema conformance scoring for the hexagon FeatureCollection.

The expected schema lives in config/hex_schema.yaml. Each rule earns partial credit, so a
handful of bad features lowers the score proportionally instead of failing the dataset.
"""

import logging
import re
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from timing import timed

log = logging.getLogger(__name__)

DEFAULT_SCHEMA = Path(__file__).resolve().parent.parent / "config" / "hex_schema.yaml"

H3_CELL_MODE = 1


class ConformanceError(RuntimeError):
    """Raised when the conformance score is below the fail threshold."""


@dataclass
class ConformanceReport:
    score: float
    status: str
    dataset_score: float
    feature_score: float
    rule_pass_rates: dict = field(default_factory=dict)


def load_schema(path: Path = DEFAULT_SCHEMA) -> dict:
    with open(path, encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def h3_resolution(index) -> int | None:
    """Decode the resolution from the H3 bit layout; None if not a valid cell index.

    Layout: bit 63 reserved (0), bits 59-62 mode (1 = cell), bits 56-58 reserved,
    bits 52-55 resolution.
    """
    try:
        value = int(index, 16)
    except (TypeError, ValueError):
        return None
    if value >> 63 or (value >> 59) & 0xF != H3_CELL_MODE:
        return None
    return (value >> 52) & 0xF


def _ring(feature):
    try:
        return feature["geometry"]["coordinates"][0]
    except (KeyError, IndexError, TypeError):
        return None


def _in_box(lat, lon, lat_rng, lon_rng) -> bool:
    return (
        isinstance(lat, (int, float))
        and isinstance(lon, (int, float))
        and lat_rng[0] <= lat <= lat_rng[1]
        and lon_rng[0] <= lon <= lon_rng[1]
    )


def _feature_check(name: str, feature, p: dict) -> bool:
    if not isinstance(feature, dict):
        return False
    props = feature.get("properties") or {}
    geom = feature.get("geometry")
    ring = _ring(feature)
    if name == "is_feature":
        return feature.get("type") == "Feature"
    if name == "index_format":
        return (
            isinstance(props.get("index"), str)
            and re.match(p["pattern"], props["index"]) is not None
        )
    if name == "index_resolution":
        return h3_resolution(props.get("index")) == p["expected"]
    if name == "resolution_property":
        return props.get("resolution") == p["expected"]
    if name == "centroid_in_bbox":
        return _in_box(props.get("centroid_lat"), props.get("centroid_lon"), p["lat"], p["lon"])
    if name == "geometry_is_polygon":
        return isinstance(geom, dict) and geom.get("type") == "Polygon" and ring is not None
    if name == "ring_closed":
        return bool(ring) and ring[0] == ring[-1]
    if name == "ring_vertex_count":
        return bool(ring) and len(ring) == p["expected"]
    if name == "coords_in_bbox":
        return bool(ring) and all(
            isinstance(c, list) and len(c) == 2 and _in_box(c[1], c[0], p["lat"], p["lon"])
            for c in ring
        )
    if name == "centroid_inside_polygon_bbox":
        if not ring:
            return False
        lat, lon = props.get("centroid_lat"), props.get("centroid_lon")
        if not (isinstance(lat, (int, float)) and isinstance(lon, (int, float))):
            return False
        lons = [c[0] for c in ring]
        lats = [c[1] for c in ring]
        return min(lats) <= lat <= max(lats) and min(lons) <= lon <= max(lons)
    raise ValueError(f"unknown feature rule: {name}")


def _dataset_check(name: str, collection: dict, p: dict) -> bool:
    features = collection.get("features") or []
    if name == "collection_type":
        return collection.get("type") == p["expected"]
    if name == "crs_name":
        return ((collection.get("crs") or {}).get("properties") or {}).get("name") == p["expected"]
    if name == "feature_count_between":
        return p["min"] <= len(features) <= p["max"]
    if name == "unique_property":
        values = [
            (f.get("properties") or {}).get(p["property"]) for f in features if isinstance(f, dict)
        ]
        return len(values) == len(set(values))
    raise ValueError(f"unknown dataset rule: {name}")


def score_collection(collection: dict, schema: dict | None = None) -> ConformanceReport:
    """Score a FeatureCollection against the schema. Returns a report (never raises)."""
    schema = schema or load_schema()
    features = collection.get("features") or []

    with timed(log, "dataset-level conformance"):
        d_total = d_earned = 0.0
        pass_rates = {}
        for rule in schema["dataset_rules"]:
            ok = _dataset_check(rule["check"], collection, rule.get("params", {}))
            d_total += rule["weight"]
            d_earned += rule["weight"] * ok
            pass_rates[rule["check"]] = 1.0 if ok else 0.0
        dataset_score = d_earned / d_total if d_total else 1.0

    with timed(log, f"feature-level conformance ({len(features)} features)"):
        rules = schema["feature_rules"]
        f_total = sum(r["weight"] for r in rules)
        passes = Counter()
        earned_sum = 0.0
        for feature in features:
            earned = 0.0
            for rule in rules:
                if _feature_check(rule["check"], feature, rule.get("params", {})):
                    earned += rule["weight"]
                    passes[rule["check"]] += 1
            earned_sum += earned / f_total
        feature_score = earned_sum / len(features) if features else 0.0
        for rule in rules:
            pass_rates[rule["check"]] = passes[rule["check"]] / len(features) if features else 0.0

    w = schema["dataset_weight"]
    score = (1 - w) * feature_score + w * dataset_score
    t = schema["thresholds"]
    status = "PASS" if score >= t["pass_at"] else "WARN" if score >= t["warn_at"] else "FAIL"
    return ConformanceReport(score, status, dataset_score, feature_score, pass_rates)


def validate(collection: dict, schema: dict | None = None) -> ConformanceReport:
    """Score, log, and raise ConformanceError on FAIL."""
    report = score_collection(collection, schema)
    for name, rate in report.rule_pass_rates.items():
        level = logging.INFO if rate == 1.0 else logging.WARNING
        log.log(level, "rule %-28s pass rate %.4f", name, rate)
    log.info(
        "conformance score %.4f (features %.4f, dataset %.4f) -> %s",
        report.score,
        report.feature_score,
        report.dataset_score,
        report.status,
    )
    if report.status == "FAIL":
        raise ConformanceError(f"conformance score {report.score:.4f} below warn threshold")
    return report
