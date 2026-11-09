# Task 5: Atlantis subsample, wind data and anonymisation

## Run it

```bash
pip install -r requirements.txt
python src/run_atlantis.py               # downloads inputs to data/, writes output/atlantis/
python src/run_atlantis.py --skip-wind   # continue with no wind columns if the source is down
python -m pytest
RUN_NETWORK=1 python -m pytest           # adds live checks of S3, the GIS server and the wind file
```

Exit codes: `0` ok, `5` wind data unavailable, `6` anonymisation self-check failed.

| Output in `output/atlantis/` | Contents | Share it? |
|---|---|---|
| `atlantis_anonymised.csv.gz` | The anonymised records | Yes, with the limits below |
| `atlantis_augmented.csv.gz` | Subsample plus exact wind, not anonymised | No |
| `held_for_manual_review.csv` | Held records with original values, not anonymised | No, restricted |
| `summary.json` | Record counts and dropped columns | Yes |

## 5.1 Subsample

**Suburb.** The centroid is computed from the `AVONDALE - WESFLEUR` polygon in the "Official
Planning Suburbs (February 2019)" layer of the Western Cape GIS server. The brief asks for "an
official suburb in the proximity of Atlantis", and this is a residential official suburb inside
Atlantis. It is also a value in the data's own `official_suburb` column.

The alternatives were rejected. No official suburb is called just ATLANTIS: that polygon exists
only in the "Major suburbs" layer, and no request carries that label. ATLANTIS INDUSTRIAL is
official, but it is an industrial area with only 444 requests within range.

The brief does not name a suburb, so several would be reasonable. The table compares the 12 official
suburbs in and around Atlantis, using the same layer, centroid method and 1852 m radius. Four more
official areas intersect the search box, but are farms or distant, with at most 152 requests in range.

| Official suburb | Requests within range | Requests labelled with it | Character |
|---|---|---|---|
| `AVONDALE - WESFLEUR` | 12,916 | 3,174 | Residential, in the town |
| `SHERWOOD` | 11,336 | 1,853 | Residential, in the town |
| `ROBINVALE` | 10,583 | 3,204 | Residential, in the town |
| `PROTEA PARK - WESFLEUR` | 10,422 | 2,065 | Residential, in the town |
| `SAXONSEA` | 9,908 | 5,116 | Residential, in the town |
| `BEACON HILL` | 8,971 | 1,098 | Residential, in the town |
| `WESFLEUR` | 6,903 | 7 | Almost no requests carry this label |
| `SAXONWOOD` | 3,549 | 14 | Almost no requests carry this label |
| `WITSAND` | 2,638 | 783 | Separate area to the south |
| `MAMRE` | 1,320 | 1,249 | Separate town to the north |
| `ATLANTIS INDUSTRIAL` | 444 | 463 | Industrial |
| `PELLA` | 331 | 327 | Separate settlement |

`AVONDALE - WESFLEUR` was chosen because it is a central residential suburb: its centroid is about
1 km from the average of the centroids of the first eight suburbs in the table. The request count
confirms that the circle covers the town's core, but it was not the reason for the choice on its
own. `ROBINVALE` is about as central and would also be defensible.

This is not the Avondale in Parow. `AVONDALE - PAROW` is a separate official suburb about 37 km
away. The January 2017 layer calls both of them `AVONDALE`, so the 2019 layer is used and the full
name is matched exactly.

The circle reaches past the suburb's boundary, so the 12,916 selected requests also carry
neighbouring labels: `SAXONSEA` 3,238, `AVONDALE - WESFLEUR` 3,174, `ROBINVALE` 2,804,
`PROTEA PARK - WESFLEUR` 1,666, `SHERWOOD` 975 and `BEACON HILL` 935, plus a few smaller ones.

**Centroid.** The polygon is downloaded at run time, projected to UTM zone 34S (EPSG:32734) so
area is measured in metres, and its area-weighted centroid is converted back to WGS84. The result
is latitude -33.566485, longitude 18.486977. Taking the centroid directly in degrees differs by
only 0.02 m here, but the projected method stays correct elsewhere.

**"Within 1 minute".** This is read as an arc-minute of distance, taken as a geodesic of at most
1852 m (one nautical mile) on the WGS84 ellipsoid. That is a convention: a true arc-minute of
latitude is about 1849 m at Atlantis. A circle is used because a minute of longitude is only about
1547 m here, so a latitude and longitude box would be lopsided.

**Result.** 12,916 of 941,634 requests are selected. The 212,364 requests with no coordinates
cannot be placed, so they are excluded.

## 5.2 Wind data

**Source.** The download link in the brief returned 404 throughout this work. The script
first tries the City's open data portal dataset "Wind" (owner `cctgis`, described as latest data
2020), then the two links from the brief. The portal copy is `Wind.xlsx`, not the `.ods` the brief
names. I could not compare them because the `.ods` is unavailable, so I am assuming the content
is the same. The parser accepts both formats.

**Handling the unreliable endpoint.** The strategy is in `src/http_fetch.py`:

| Step | Why |
|---|---|
| Retry up to 4 times with exponential backoff and jitter | Most failures on public endpoints are short-lived, and jitter avoids hammering a struggling server |
| Try other URLs in order | One dead host should not stop the run. This is what actually saved the run: the brief's link is dead |
| Validate every download | An error page returned with HTTP 200, or a truncated file, must not be parsed or cached |
| Keep the last good copy and fall back to it, with a loud warning | The data is a closed year of readings, so an old copy is not stale in any way that matters |
| With no source and no copy, stop with exit code 5 | Silently producing output without wind would look like a success |

`--skip-wind` exists so that leaving wind out is an explicit choice by the person running it. One
known cost: the code does not separate a permanent 404 from a temporary failure, so both are
retried. With three URLs, a run where every source is down should wait roughly 30 seconds before
failing, going by the backoff settings (not measured with all three down).

**Site.** The file has seven sites side by side. Only the columns labelled Atlantis AQM Site are
used. If the Atlantis columns cannot be identified the script raises an error rather than guessing,
because another site's wind would look perfectly plausible.

**Join.** Each request gets the nearest usable reading to its creation time, within 1 hour.
Readings are hourly averages, so the nearest is normally within 30 minutes. The hour lets a request
use the next reading when the nearest is missing.

**Coverage is the main limitation.** The Atlantis sensor only reported for 26.5% of 2020, so only
**5,605 of 12,916 requests (43%)** get a reading. The rest stay blank. No other site is substituted,
because the brief asks for Atlantis specifically.

| Month | Jan | Feb | Mar | Apr to Sep | Oct | Nov | Dec |
|---|---|---|---|---|---|---|---|
| Requests with wind | 2329 | 1503 | 752 | 0 | 789 | 232 | 0 |

Any analysis of this data should treat wind as available for those months only, not as a full year.

**Assumptions the file does not confirm.** Times are taken as South African local time, and a
reading labelled 09:00 is treated as describing 09:00. The file says "AVG 1 Hr" without saying
whether that hour starts or ends at the label, so a match could be up to about an hour off.

## 5.3 Why this data is now anonymised

No names, addresses or account numbers remain, and nothing links a row back to the City's systems.
The original request numbers are replaced by random ids in shuffled order, with no mapping kept.

Location is limited to an H3 resolution 8 hexagon (average edge about 530 m, area about 0.74 km²).
Exact coordinates and the distance to the centroid are removed. `official_suburb` is removed too,
because 12 of the 19 hexagons in the subsample span more than one suburb, so it could place a
request more finely than the required 500 m.

Time is limited to 6-hour buckets in local time. The exact wind values are also removed, because
the wind file is public: looking up a request's exact speed and direction would reveal the hour it
was made, undoing the bucket. They are replaced by the mean for the whole bucket, the same for every
request in it, so wind says nothing about when within the bucket a request was made. The
self-check stops the run if any bucket has more than one wind value.

Records that could still identify someone are held back for a person to review: 1,550 of 12,916
(12.0%). Of these, 190 contain a category value seen fewer than 5 times, since a one-off request
type can identify a household by itself. The other 1,360 are sensitive requests (billing,
valuations, housing, health, meter queries) shared by fewer than 3 records in the same hexagon,
bucket and request group. These describe a household's finances, tenure or health, so they carry
the most harm if singled out. The rules repeat until stable, because holding records back can make
other values rare. The list of sensitive categories is a judgement call and should be reviewed.

**What this does not achieve.** The required precision leaves small groups. Of the 11,366 anonymised
rows, 29.0% are unique on hexagon and 6-hour bucket alone, 58.5% once the request code is added, and
73.8% with the completion bucket too. The median group on hexagon and bucket is 1 record.

So someone who already knows that a neighbour reported a specific problem, on a certain day, could
probably find that row. What they would learn is limited to what the row adds: when it was
completed, and its category. A stranger cannot go from a row to a person without that outside
knowledge, because the row holds no identity. Coarsening further would remove this, but the brief
requires this precision, so the data should be described as low risk rather than risk free, and
shared with that stated.

## Validation

- `tests/test_atlantis_subsample.py`, `test_wind.py`, `test_http_fetch.py` and `test_anonymise.py`
  cover the behaviour above without needing a network.
- `tests/test_task_runners.py` runs `run_atlantis.py` end to end, with only the two downloads
  replaced, for every exit code and for `--skip-wind`.
- The live test checks the real wind file against the summary rows printed in the file: 8,784
  hourly rows, 2,324 valid Atlantis readings, speed from 0.4 to 11.2 m/s, direction from 0.1 to 358
  degrees.
- During development the join was recalculated separately from the raw file, using the previous
  subsample of 10,292 requests. The match count was the same (4,401) with no differences in speed
  or direction. That separate check was not repeated for the current subsample.
- At run time, `check_precision` stops the run if an identifying column survives, a time is off a
  6-hour boundary, wind varies within a bucket, or a record id repeats or matches an original.

## Observed run (this machine)

| Step | Time |
|---|---|
| Read `sr_hex.csv.gz` (already cached) | 1.4 s |
| Distance filter on 941,634 requests | 0.5 s |
| Parse the wind file | 0.8 s |
| Anonymise 12,916 records | 0.1 s |
| Total, including downloading the polygon and wind file | 4.2 s |

The distance filter runs as one array operation rather than row by row, and only rows with usable
coordinates are measured.
