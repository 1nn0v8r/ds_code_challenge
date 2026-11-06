# Task 1: Data extraction with S3 Select

## Run it

```bash
pip install -r requirements.txt
python src/run_extraction.py
python -m pytest            # unit tests, no network
RUN_NETWORK=1 python -m pytest   # adds a live test against S3
```

Output is written to `output/city-hex-polygons-8-extracted.geojson`. Exit codes: `0` ok,
`2` conformance below the fail threshold, `3` extract differs from the reference file.

## Approach

`city-hex-polygons-8-10.geojson` is 108 MB and holds resolutions 8, 9 and 10. We only
want resolution 8 (about 2% of it).

S3 Select's JSON reader rejects any record over 1 MiB, and this file is a single JSON
document, so `Type: DOCUMENT` and `Type: LINES` both fail with `OverMaxRecordSize`.
The file is pretty-printed with one feature per line, so we read it as delimited text
instead (tab delimiter and backtick quote character, neither of which occurs in the file)
and filter lines with `LIKE '%"resolution": 8 }%'`. The filter runs inside S3, so only
about 2 MB comes back instead of 108 MB. One request also returns the `type` and `crs`
header lines, which the dataset-level checks need.

Caveat: this relies on the file staying one feature per line. If the layout changes, the
feature-count rule and the reference comparison fail loudly rather than silently returning
partial data.

## Validation

1. Reference check: every feature is compared by H3 index, geometry and centroid against
   `city-hex-polygons-8.geojson`. Any missing, extra or different hexagon exits with `3`.
2. Schema conformance score, defined in `config/hex_schema.yaml`.

## Conformance score

`score = 0.8 * mean(per-feature weighted pass rate) + 0.2 * (weighted dataset pass rate)`

Each rule has a weight and earns partial credit, so the score is continuous between 0 and
1 rather than pass/fail. Thresholds (also in the YAML):

| Score | Status | Behaviour |
|---|---|---|
| >= 0.99 | PASS | continue |
| >= 0.95 | WARN | continue, failing rules logged as warnings |
| < 0.95 | FAIL | pipeline stops with exit code 2 |

Why these thresholds: the file is a small, machine-generated reference grid, so anything
short of near-perfect signals a producer change. With 3832 features a 0.99 floor tolerates
roughly 1% bad features, enough to notice drift without blocking on a single odd polygon.
Below 0.95 a meaningful share of the grid is malformed, and joining service requests to it
would silently misassign records. Dataset-level rules are weighted so that a single duplicate
H3 index (a primary key violation) scores about 0.914 and FAILs on its own. This is
deliberate: a duplicate would make the later join assign one request to two hexagons.

## Observed run (this machine, af-south-1)

| Step | Time |
|---|---|
| S3 Select, resolution 8 | about 7.5 s (scanned 108.3 MB, returned 2.0 MB) |
| Parse 3832 features | 0.03 s |
| Dataset and feature conformance | under 0.05 s |
| Download reference | 0.2 s |
| Total | about 8.5 s |

S3 Select time is dominated by the server scanning 108 MB; it cannot be reduced from the
client. The alternative, downloading the full file and parsing in memory, moves 50x more
data.
