"""Task 5.2: download the Atlantis wind data and attach it to service requests.

Source: the download link in the brief returned 404 when this was built (checked 2026-11-07),
so the City's own open data portal copy is tried first and the brief's links are kept as
fallbacks. The portal copy is Wind.xlsx, not the .ods the brief names; either is accepted.

Real layout: one sheet with a title row, a row of site names, a row of column names (Wind Dir
V, Wind Speed V) and a row of units, then hourly readings from seven sites side by side,
then summary rows (Minimum, Avg, ...). Missing readings are text such as NoData or Down.
Columns are found by keyword, not position, and a separate Date and Time column pair is
supported. Timestamps are taken to be local South African time unless they carry a zone, and
text dates are read day first.

Site selection: the brief wants the Atlantis site, so the sheet or columns naming Atlantis
are used. If they cannot be told apart this raises rather than guessing, because another
site's wind would look perfectly plausible.

Coverage: the Atlantis sensor only reported for 26.5% of 2020 (January to March and October
to November), so many requests legitimately get no wind. No other site is substituted.
"""

import io
import logging
import re
import zipfile
import zlib
from pathlib import Path

import numpy as np
import pandas as pd

from http_fetch import fetch_bytes
from timing import timed

log = logging.getLogger(__name__)

WIND_URLS = [
    "https://www.arcgis.com/sharing/rest/content/items/31ef242a23484e79bbb19d6b29203179/data",
    "https://www.capetown.gov.za/_layouts/OpenDataPortalHandler/DownloadHandler.ashx"
    "?DocumentName=Wind_direction_and_speed_2020.ods&DatasetDocument="
    "https%3A%2F%2Fcityapps.capetown.gov.za%2Fsites%2Fopendatacatalog%2FDocuments%2F"
    "Wind%2FWind_direction_and_speed_2020.ods",
    "https://cityapps.capetown.gov.za/sites/opendatacatalog/Documents/Wind/"
    "Wind_direction_and_speed_2020.ods",
]
ODS_MIMETYPE = b"application/vnd.oasis.opendocument.spreadsheet"
CACHE_FILE = "wind_2020_spreadsheet"
LOCAL_TZ = "Africa/Johannesburg"
SITE = "atlantis"
HEADER_SCAN_ROWS = 30
# Readings are hourly averages, so the nearest one is normally within 30 minutes. An hour
# also lets a request use the next reading when the nearest is missing, but no further.
DEFAULT_TOLERANCE = pd.Timedelta(hours=1)

WIND_COLUMNS = ["wind_timestamp", "wind_speed", "wind_direction"]


def validate_spreadsheet(payload: bytes) -> None:
    """Reject HTML error pages and truncated downloads before they reach the cache."""
    if not payload.startswith(b"PK"):
        raise ValueError(f"payload is not a spreadsheet file (got {payload[:20]!r})")
    try:
        with zipfile.ZipFile(io.BytesIO(payload)) as zf:
            names = set(zf.namelist())
            is_ods = "mimetype" in names and zf.read("mimetype").strip() == ODS_MIMETYPE
            if not (is_ods or "xl/workbook.xml" in names):
                raise ValueError("zip is not an .ods or .xlsx spreadsheet")
            bad = zf.testzip()
            if bad:
                raise ValueError(f"corrupt member in spreadsheet: {bad}")
    except (zipfile.BadZipFile, zlib.error) as exc:
        raise ValueError(f"invalid spreadsheet: {exc}") from exc


def _pick_site_column(columns: list[int], cells: list[str], label: str) -> int:
    """Return the only column, or the one naming the site. Raises rather than guessing."""
    if len(columns) == 1:
        return columns[0]
    named = [j for j in columns if SITE in cells[j]]
    if len(named) != 1:
        raise ValueError(f"{len(columns)} wind {label} columns and cannot tell which is Atlantis")
    return named[0]


def _find_header(raw: pd.DataFrame) -> tuple[int, dict] | None:
    for i in range(min(HEADER_SCAN_ROWS, len(raw))):
        # A column's label includes the row above, which is where the real file names the
        # site. Filled forward so a site name in a merged cell reaches all its columns.
        above = raw.iloc[i - 1].astype(object).ffill() if i else [None] * raw.shape[1]
        cells = [
            " ".join(str(v).strip().lower() for v in pair if pd.notna(v))
            for pair in zip(above, raw.iloc[i], strict=True)
        ]
        speeds, directions, found = [], [], {}
        for j, text in enumerate(cells):
            if "speed" in text:
                speeds.append(j)
            elif re.search(r"\bdir|direction", text):
                directions.append(j)
            elif (
                re.search(r"\btime(stamp)?\b", text) and "date" not in text and "time" not in found
            ):
                found["time"] = j
            elif "date" in text and "date" not in found:
                found["date"] = j
        if speeds and directions and found:
            found["speed"] = _pick_site_column(speeds, cells, "speed")
            found["direction"] = _pick_site_column(directions, cells, "direction")
            return i, found
    return None


def _parse_timestamps(stamp: pd.Series) -> pd.Series:
    """Parse year-first values as ISO and anything else day first; unparseable becomes NaT.

    The two are kept apart because pandas' dayfirst=True also swaps ISO dates, turning
    2020-01-02 into 1 February. Date cells arrive as timestamps, whose text form is ISO.
    """
    text = stamp.astype(object).map(lambda v: "" if pd.isna(v) else str(v).strip())
    iso = text.str.match(r"\d{4}-\d{2}-\d{2}")
    if iso.all():
        return pd.to_datetime(text, format="ISO8601", errors="coerce")
    day_first = pd.to_datetime(text.where(~iso), format="mixed", dayfirst=True, errors="coerce")
    if not iso.any():
        return day_first
    return pd.to_datetime(text.where(iso), format="ISO8601", errors="coerce").fillna(day_first)


def parse_wind(payload: bytes, tz: str = LOCAL_TZ) -> pd.DataFrame:
    """Return a DataFrame with wind_timestamp (tz-aware), wind_speed, wind_direction."""
    sheets = pd.read_excel(io.BytesIO(payload), sheet_name=None, header=None)
    site_sheets = {name: raw for name, raw in sheets.items() if SITE in str(name).lower()}
    named_site = bool(site_sheets)
    frames = []
    for name, raw in (site_sheets or sheets).items():
        hit = _find_header(raw)
        if hit is None:
            log.info("sheet %r: no wind header found, skipping", name)
            continue
        row, cols = hit
        body = raw.iloc[row + 1 :]
        if "date" in cols and "time" in cols:
            stamp = (
                body.iloc[:, cols["date"]].astype(str).str.split(" ").str[0]
                + " "
                + body.iloc[:, cols["time"]].astype(str).str.split(" ").str[-1]
            )
        else:
            stamp = body.iloc[:, cols.get("date", cols.get("time"))]
        ts = _parse_timestamps(stamp)
        frames.append(
            pd.DataFrame(
                {
                    "wind_timestamp": ts,
                    "wind_speed": pd.to_numeric(body.iloc[:, cols["speed"]], errors="coerce"),
                    "wind_direction": pd.to_numeric(
                        body.iloc[:, cols["direction"]], errors="coerce"
                    ),
                }
            )
        )
        log.info("sheet %r: header at row %d, %d data rows", name, row, len(body))
    if not frames:
        raise ValueError("no sheet with date/time, wind speed and wind direction columns")

    wind = pd.concat(frames, ignore_index=True)
    # Sheets covering different periods (one per month, say) are fine to combine. The same
    # times on several sheets means several sites, and none of them is named Atlantis.
    per_sheet = sum(f["wind_timestamp"].nunique() for f in frames)
    if not named_site and per_sheet != wind["wind_timestamp"].nunique():
        raise ValueError("wind sheets overlap in time and none is named Atlantis")
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
        len(wind),
        wind.wind_timestamp.min(),
        wind.wind_timestamp.max(),
        wind.wind_speed.isna().sum(),
        wind.wind_direction.isna().sum(),
    )
    return wind


def load_wind(cache_dir: Path, urls=None, **fetch_kwargs) -> pd.DataFrame:
    payload, source = fetch_bytes(
        urls or WIND_URLS, Path(cache_dir) / CACHE_FILE, validate_spreadsheet, **fetch_kwargs
    )
    with timed(log, f"parse wind data (from {'cache' if source == 'cache' else 'network'})"):
        return parse_wind(payload)


def attach_wind(
    sr: pd.DataFrame, wind: pd.DataFrame, tolerance: pd.Timedelta = DEFAULT_TOLERANCE
) -> pd.DataFrame:
    """Attach the nearest wind reading within `tolerance` of each request's creation time.

    Nearest (either side) is used because a reading describes the time around it, not a
    state that persists until the next reading. Hours with no usable reading are skipped, so
    a request never matches a blank while a real reading is in tolerance. Rows with no
    reading in tolerance get nulls, and the count is logged.
    """
    wind = wind.dropna(subset=["wind_speed", "wind_direction"], how="all")
    created = pd.to_datetime(sr["creation_timestamp"], utc=True, errors="coerce")
    bad = int(created.isna().sum())
    if bad:
        log.warning("%d requests have an unparseable creation_timestamp; no wind attached", bad)

    # merge_asof needs both sides sorted and returns a fresh index, so carry the row
    # position through the merge and use it to put results back in the original order.
    # .array keeps the datetime dtype (to_numpy can give objects, which merge_asof rejects),
    # and as_unit stops differing timestamp resolutions being rejected as incompatible.
    left = pd.DataFrame({"_pos": np.arange(len(sr)), "_created": created.dt.as_unit("us").array})
    left = left.dropna(subset=["_created"]).sort_values("_created")
    right = pd.DataFrame(
        {
            "_wind_utc": wind["wind_timestamp"].dt.tz_convert("UTC").dt.as_unit("us").array,
            "wind_timestamp": wind["wind_timestamp"].array,
            "wind_speed": wind["wind_speed"].to_numpy(),
            "wind_direction": wind["wind_direction"].to_numpy(),
        }
    ).sort_values("_wind_utc")
    merged = (
        pd.merge_asof(
            left,
            right,
            left_on="_created",
            right_on="_wind_utc",
            direction="nearest",
            tolerance=tolerance,
        )
        .set_index("_pos")
        .sort_index()
    )

    result = sr.copy()
    aligned = merged.reindex(np.arange(len(sr)))
    for col in WIND_COLUMNS:
        result[col] = aligned[col].array
    matched = int(result["wind_speed"].notna().sum())
    log.info(
        "wind join: %d of %d requests matched within %s (%d unmatched)",
        matched,
        len(result),
        tolerance,
        len(result) - matched,
    )
    return result
