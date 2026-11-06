# Task 2: Join service requests to H3 resolution 8 hexagons

## Run it

```bash
pip install -r requirements.txt
python src/sr_join.py                              # downloads inputs to data/, writes output/
python src/sr_join.py --max-join-error-rate 0.001  # the default, shown explicitly
python -m pytest
```

Output: `output/sr_hex_joined.csv.gz`, the same 16 columns as `sr_hex.csv.gz`. Exit codes:
`0` ok, `3` output differs unexpectedly from the reference, `4` join error rate above the
threshold.

## Method

1. Download `sr.csv.gz`, `city-hex-polygons-8.geojson` and `sr_hex.csv.gz` (retry with
   exponential backoff, cached in `data/`).
2. Compute the resolution 8 H3 cell for each request's latitude and longitude, then keep
   it only if that cell is in the City grid from `city-hex-polygons-8.geojson`. Otherwise
   the index is `0`.
3. Requests with empty coordinates get `0`, as the task requires.

Why H3 lookup rather than a point-in-polygon join: each hexagon's id is its own address,
so the cell is computed directly from the coordinate (0.5 s for 729,270 points) and the
GeoJSON is used as the authoritative set of valid City cells. I also benchmarked a
spatial-index polygon join (shapely STRtree, 0.7 s); it produced 29 disagreements with
`sr_hex.csv.gz`, 3 of which are the outside-grid rows below. I did not investigate the other
26; they are probably points near hexagon edges, where the GeoJSON polygons (straight
lines between corners) may not match H3's own cell boundaries exactly. The H3 lookup had
zero disagreements with the reference on the same rows, which is why it was chosen.

## What counts as a failure

| Case | Index | Counted as join failure? |
|---|---|---|
| Latitude and longitude both empty | `0` | No (required by the task, 212,364 rows) |
| Only one of the two empty | `0` | No, treated as missing and reported as `partial` |
| Non-numeric coordinate | `0` | No, treated as missing |
| Coordinate outside valid lat/lon range | `0` | Yes (`invalid`) |
| Valid coordinate, cell not in City grid | `0` | Yes (`outside_grid`) |

The error rate is `failed / requests with coordinates`. Rows with no coordinates are not
failures, so they cannot hide or dilute real join problems.

## Error threshold: 0.1%

Observed on the full year: 3 of 729,270 located requests fail (0.0004%). I set the
threshold at 0.1% (default `--max-join-error-rate 0.001`, about 730 rows).

- Bad geocodes at the edge of the City are normal, and a few hundred of them do not
  change per-hexagon counts materially. The threshold is about 250 times what we see, so
  routine noise never trips it.
- A systematic fault (swapped latitude and longitude, wrong resolution, a different grid
  file, a shifted CRS) sends a large share of rows outside the grid, orders of magnitude
  above 0.1%. It is better to stop the run than to publish a quietly wrong dataset.
- The denominator is located rows only, so the threshold keeps its meaning if the share of
  requests without coordinates changes.

The threshold is a judgement call, not a derived value. Tighten it if the City wants
stricter data quality, loosen it only with a reason.

## Validation against `sr_hex.csv.gz`

Rows are matched on `notification_number` (checked unique in both files).

- 941,634 of 941,634 rows compared; none only in one file.
- Every other column is cell-for-cell identical to the reference (checked on the written
  file).
- `h3_level8_index` differs in exactly 3 rows. In all three the reference holds a real H3
  cell (`88ad36c629fffff`, `88ad361b51fffff`) that is not in `city-hex-polygons-8.geojson`,
  and we output `0`. The task says each request is assigned to a hexagon of the City grid,
  so `0` is the consistent answer; they are logged as expected differences. Any other
  difference makes the run exit with `3`.

## Data quirks handled

- The README calls the columns `Latitude` and `Longitude`; the files use lowercase. The
  lookup is case-insensitive.
- `sr.csv.gz` has a leaked pandas row-number column (`Unnamed: 0`) that `sr_hex.csv.gz`
  does not. It is dropped so the output has the reference's columns.
- Everything is read as text. Type inference would strip the leading zeros from
  `notification_number`, add `.0` to `reference_number`, and turn the literal text `None`
  in `cause_code` into a blank. Reading as text also keeps `0` a string in the index.

## Observed run (this machine)

| Step | Time |
|---|---|
| Download 3 files (first run only, cached after) | about 10 s |
| Read `sr.csv.gz` | 2.1 s |
| Assign H3 index to 941,634 rows | 0.5 s |
| Read reference and compare | 1.6 s |
| Write gzip output (compression level 1) | 4.2 s |
| Total with cached inputs | about 8.7 s |

Writing dominates; gzip level 1 was chosen over the default level 9 to cut that step.
