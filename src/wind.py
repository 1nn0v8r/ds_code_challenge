"""Task 5.2: download the Atlantis wind data and attach it to service requests.

STATUS: the parser is written against an ASSUMED layout, because the City's endpoint was
returning 404 for every URL variant while this was built (checked 2026-11-06). It has only
been tested against a small synthetic file generated in tests/. Run it against the real
file before trusting it; see docs/task5_atlantis.md.

Assumed layout: an OpenDocument spreadsheet with a header row (possibly below some title
rows) that has a date/time column, a wind speed column and a wind direction column.
Columns are found by keyword, not position, and a separate Date and Time column pair is
supported. Timestamps are taken to be local South African time unless they carry a zone.
"""
import io
import logging
import re
import zipfile
from pathlib import Path

import numpy as np
import pandas as pd

from http_fetch import fetch_bytes
from timing import timed

log = logging.getLogger(__name__)

WIND_URLS = [
    "https://www.capetown.gov.za/_layouts/OpenDataPortalHandler/DownloadHandler.ashx"
    "?DocumentName=Wind_direction_and_speed_2020.ods&DatasetDocument="
    "https%3A%2F%2Fcityapps.capetown.gov.za%2Fsites%2Fopendatacatalog%2FDocuments%2F"
    "Wind%2FWind_direction_and_speed_2020.ods",
    "https://cityapps.capetown.gov.za/sites/opendatacatalog/Documents/Wind/"
    "Wind_direction_and_speed_2020.ods",
]
ODS_MIMETYPE = b"application/vnd.oasis.opendocument.spreadsheet"
LOCAL_TZ = "Africa/Johannesburg"
HEADER_SCAN_ROWS = 30
# Default match window for attaching a reading to a request. Set from the reading interval
# once the real file is seen (hourly data would justify 30 to 60 minutes).
DEFAULT_TOLERANCE = pd.Timedelta(hours=1)

WIND_COLUMNS = ["wind_timestamp", "wind_speed", "wind_direction"]


def validate_ods(payload: bytes) -> None:
    """Reject HTML error pages and truncated downloads before they reach the cache."""
    if not payload.startswith(b"PK"):
        raise ValueError("payload is not a zip/ODS file (got %r)" % payload[:20])
    try:
        with zipfile.ZipFile(io.BytesIO(payload)) as zf:
            if zf.read("mimetype").strip() != ODS_MIMETYPE:
                raise ValueError("zip is not an OpenDocument spreadsheet")
            bad = zf.testzip()
            if bad:
                raise ValueError(f"corrupt member in ODS: {bad}")
    except (zipfile.BadZipFile, KeyError) as exc:
        raise ValueError(f"invalid ODS: {exc}") from exc


def _find_header(raw: pd.DataFrame) -> tuple[int, dict] | None:
    for i in range(min(HEADER_SCAN_ROWS, len(raw))):
        cells = [str(v).strip().lower() if pd.notna(v) else "" for v in raw.iloc[i]]
        found = {}
        for j, text in enumerate(cells):
            if "speed" in text and "speed" not in found:
                found["speed"] = j
            elif re.search(r"\bdir|direction", text) and "direction" not in found:
                found["direction"] = j
            elif re.fullmatch(r".*\btime\b.*", text) and "date" not in text and "time" not in found:
                found["time"] = j
            elif "date" in text and "date" not in found:
                found["date"] = j
        if "speed" in found and "direction" in found and ("date" in found or "time" in found):
            return i, found
    return None


def parse_wind(payload: bytes, tz: str = LOCAL_TZ) -> pd.DataFrame:
    """Return a DataFrame with wind_timestamp (tz-aware), wind_speed, wind_direction."""
    sheets = pd.read_excel(io.BytesIO(payload), engine="odf", sheet_name=None, header=None)
    frames = []
    for name, raw in sheets.items():
        hit = _find_header(raw)
        if hit is None:
            log.info("sheet %r: no wind header found, skipping", name)
            continue
        row, cols = hit
        body = raw.iloc[row + 1:]
        if "date" in cols and "time" in cols:
            stamp = body.iloc[:, cols["date"]].astype(str).str.split(" ").str[0] + " " + \
                body.iloc[:, cols["time"]].astype(str).str.split(" ").str[-1]
        else:
            stamp = body.iloc[:, cols.get("date", cols.get("time"))]
        ts = pd.to_datetime(stamp, errors="coerce")
        frames.append(pd.DataFrame({
            "wind_timestamp": ts,
            "wind_speed": pd.to_numeric(body.iloc[:, cols["speed"]], errors="coerce"),
            "wind_direction": pd.to_numeric(body.iloc[:, cols["direction"]], errors="coerce"),
        }))
        log.info("sheet %r: header at row %d, %d data rows", name, row, len(body))
    if not frames:
        raise ValueError("no sheet with date/time, wind speed and wind direction columns")

    wind = pd.concat(frames, ignore_index=True)
    before = len(wind)
    wind = wind.dropna(subset=["wind_timestamp"])
    if wind.empty:
        raise ValueError("no parseable timestamps in wind data")
    if wind["wind_timestamp"].dt.tz is None:
        # nonexistent/ambiguous are NaT-ed rather than guessed (DST is not observed in
        # South Africa, so this should never trigger).
        wind["wind_timestamp"] = wind["wind_timestamp"].dt.tz_localize(
            tz, nonexistent="NaT", ambiguous="NaT"
        )
        wind = wind.dropna(subset=["wind_timestamp"])
    dropped = before - len(wind)
    if dropped:
        log.warning("dropped %d wind rows with unparseable timestamps", dropped)

    dups = wind["wind_timestamp"].duplicated().sum()
    if dups:
        log.warning("%d duplicate wind timestamps, keeping the first of each", dups)
        wind = wind.drop_duplicates("wind_timestamp", keep="first")
    wind = wind.sort_values("wind_timestamp").reset_index(drop=True)
    log.info(
        "parsed %d wind readings %s to %s (speed null %d, direction null %d)",
        len(wind), wind.wind_timestamp.min(), wind.wind_timestamp.max(),
        wind.wind_speed.isna().sum(), wind.wind_direction.isna().sum(),
    )
    return wind


def load_wind(cache_dir: Path, urls=None, **fetch_kwargs) -> pd.DataFrame:
    payload, source = fetch_bytes(
        urls or WIND_URLS, Path(cache_dir) / "Wind_direction_and_speed_2020.ods", validate_ods,
        **fetch_kwargs,
    )
    with timed(log, f"parse wind data (from {'cache' if source == 'cache' else 'network'})"):
        return parse_wind(payload)


def attach_wind(
    sr: pd.DataFrame, wind: pd.DataFrame, tolerance: pd.Timedelta = DEFAULT_TOLERANCE
) -> pd.DataFrame:
    """Attach the nearest wind reading within `tolerance` of each request's creation time.

    Nearest (either side) is used because a reading is a measurement at a point in time,
    not a state that persists until the next reading. Rows with no reading in tolerance get
    nulls, and the count is logged.
    """
    created = pd.to_datetime(sr["creation_timestamp"], utc=True, errors="coerce")
    bad = int(created.isna().sum())
    if bad:
        log.warning("%d requests have an unparseable creation_timestamp; no wind attached", bad)

    # merge_asof needs both sides sorted and returns a fresh index, so carry the row
    # position through the merge and use it to put results back in the original order.
    left = pd.DataFrame({"_pos": np.arange(len(sr)), "_created": created.to_numpy()})
    left = left.dropna(subset=["_created"]).sort_values("_created")
    right = pd.DataFrame({
        "_wind_utc": wind["wind_timestamp"].dt.tz_convert("UTC").to_numpy(),
        "wind_timestamp": wind["wind_timestamp"].to_numpy(),
        "wind_speed": wind["wind_speed"].to_numpy(),
        "wind_direction": wind["wind_direction"].to_numpy(),
    }).sort_values("_wind_utc")
    merged = pd.merge_asof(
        left, right, left_on="_created", right_on="_wind_utc",
        direction="nearest", tolerance=tolerance,
    ).set_index("_pos").sort_index()

    result = sr.copy()
    aligned = merged.reindex(np.arange(len(sr)))
    for col in WIND_COLUMNS:
        result[col] = aligned[col].to_numpy()
    matched = int(result["wind_speed"].notna().sum())
    log.info(
        "wind join: %d of %d requests matched within %s (%d unmatched)",
        matched, len(result), tolerance, len(result) - matched,
    )
    return result
