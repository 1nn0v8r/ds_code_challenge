"""Read the H3 resolution 8 hexagons out of city-hex-polygons-8-10.geojson with S3 Select.

Why the odd serialization: S3 Select's JSON reader rejects records over 1 MiB, and this
file is one JSON document (a single ~108 MB record). The file is pretty-printed with one
feature per line, so we read it as tab-delimited text (no tabs exist in the file, and a
backtick quote character never appears) and filter lines server side. Only ~2 MB of the
108 MB is returned over the wire.
"""

import json
import logging

from timing import timed

log = logging.getLogger(__name__)

SOURCE_KEY = "city-hex-polygons-8-10.geojson"

# One request returns the header lines (type, crs) and every feature at the resolution.
QUERY_TEMPLATE = (
    "SELECT s._1 FROM S3Object s "
    "WHERE s._1 LIKE '%\"resolution\": {res} }}%' "
    'OR s._1 LIKE \'%"type": "FeatureCollection"%\' '
    "OR s._1 LIKE '%\"crs\":%'"
)


def select_lines(s3, bucket: str, key: str, expression: str) -> tuple[list[str], dict]:
    """Run an S3 Select query and return (non-empty lines, stats details)."""
    resp = s3.select_object_content(
        Bucket=bucket,
        Key=key,
        ExpressionType="SQL",
        Expression=expression,
        InputSerialization={
            "CSV": {
                "FileHeaderInfo": "NONE",
                "FieldDelimiter": "\t",
                "QuoteCharacter": "`",
                "RecordDelimiter": "\n",
            }
        },
        OutputSerialization={
            "CSV": {
                "FieldDelimiter": "\t",
                "RecordDelimiter": "\n",
                "QuoteCharacter": "`",
                "QuoteFields": "ASNEEDED",
            }
        },
    )
    chunks, stats = [], {}
    for event in resp["Payload"]:
        if "Records" in event:
            chunks.append(event["Records"]["Payload"])
        elif "Stats" in event:
            stats = event["Stats"]["Details"]
    text = b"".join(chunks).decode("utf-8")
    return [ln for ln in text.split("\n") if ln.strip()], stats


def parse_collection(lines: list[str]) -> dict:
    """Rebuild a FeatureCollection dict from the lines S3 Select returned."""
    collection: dict = {"features": []}
    for raw in lines:
        line = raw.strip().rstrip(",")
        if line.startswith('{ "type": "Feature"'):
            collection["features"].append(json.loads(line))
        elif line.startswith('"type"'):
            collection["type"] = json.loads("{" + line + "}")["type"]
        elif line.startswith('"crs"'):
            collection["crs"] = json.loads("{" + line + "}")["crs"]
    return collection


def extract_hexes(s3, bucket: str, key: str = SOURCE_KEY, resolution: int = 8) -> dict:
    """Return the FeatureCollection of hexagons at one H3 resolution."""
    expression = QUERY_TEMPLATE.format(res=resolution)
    with timed(log, f"S3 Select resolution {resolution} from {key}"):
        lines, stats = select_lines(s3, bucket, key, expression)
    scanned = stats.get("BytesScanned", 0)
    returned = stats.get("BytesReturned", 0)
    log.info(
        "S3 Select scanned %.1f MB, returned %.1f MB (%.1f%% of scanned)",
        scanned / 1e6,
        returned / 1e6,
        100 * returned / scanned if scanned else 0,
    )
    with timed(log, "parse features"):
        collection = parse_collection(lines)
    log.info("extracted %d features", len(collection["features"]))
    return collection
