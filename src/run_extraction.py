"""Task 1 entry point: extract H3 res 8 hexes with S3 Select, validate, and write them out.

Usage:  python src/run_extraction.py [--out output/city-hex-polygons-8-extracted.geojson]

Exit code is non-zero if the extract does not match the reference file or if schema
conformance falls below the fail threshold.
"""
import argparse
import json
import logging
import sys
from pathlib import Path

from aws_client import BUCKET, make_s3_client
from conformance import ConformanceError, validate
from extract import extract_hexes
from timing import timed

log = logging.getLogger("run_extraction")

REFERENCE_KEY = "city-hex-polygons-8.geojson"
DEFAULT_OUT = Path("output") / "city-hex-polygons-8-extracted.geojson"


def load_reference(s3, bucket: str = BUCKET, key: str = REFERENCE_KEY) -> dict:
    with timed(log, f"download reference {key}"):
        body = s3.get_object(Bucket=bucket, Key=key)["Body"].read()
    return json.loads(body)


def compare_to_reference(extracted: dict, reference: dict) -> dict:
    """Compare feature sets by H3 index, then compare geometry and centroid per index."""
    ext = {f["properties"]["index"]: f for f in extracted["features"]}
    ref = {f["properties"]["index"]: f for f in reference["features"]}
    missing = sorted(ref.keys() - ext.keys())
    extra = sorted(ext.keys() - ref.keys())
    geometry_mismatch = [
        idx for idx in ref.keys() & ext.keys()
        if ref[idx]["geometry"] != ext[idx]["geometry"]
        or ref[idx]["properties"]["centroid_lat"] != ext[idx]["properties"]["centroid_lat"]
        or ref[idx]["properties"]["centroid_lon"] != ext[idx]["properties"]["centroid_lon"]
    ]
    return {
        "reference_count": len(ref),
        "extracted_count": len(ext),
        "missing": missing,
        "extra": extra,
        "geometry_mismatch": sorted(geometry_mismatch),
        "matches": not (missing or extra or geometry_mismatch),
    }


def write_geojson(collection: dict, out: Path) -> None:
    out.parent.mkdir(parents=True, exist_ok=True)
    with open(out, "w", encoding="utf-8") as fh:
        json.dump(collection, fh)
    log.info("wrote %s (%d features)", out, len(collection["features"]))


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(name)s: %(message)s")

    with timed(log, "total pipeline"):
        s3 = make_s3_client()
        extracted = extract_hexes(s3, BUCKET)

        try:
            report = validate(extracted)
        except ConformanceError as exc:
            log.error("%s", exc)
            return 2

        comparison = compare_to_reference(extracted, load_reference(s3))
        log.info(
            "reference check: reference=%d extracted=%d missing=%d extra=%d geometry_mismatch=%d",
            comparison["reference_count"], comparison["extracted_count"],
            len(comparison["missing"]), len(comparison["extra"]),
            len(comparison["geometry_mismatch"]),
        )
        if not comparison["matches"]:
            log.error("extract does not match %s", REFERENCE_KEY)
            return 3

        write_geojson(extracted, args.out)
    log.info("done, conformance %s (%.4f)", report.status, report.score)
    return 0


if __name__ == "__main__":
    sys.exit(main())
