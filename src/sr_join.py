"""Task 2 entry point: assign every service request to one H3 resolution 8 hexagon.

Usage:  python src/sr_join.py [--max-join-error-rate 0.001] [--out output/sr_hex_joined.csv.gz]

Method: compute the H3 res 8 cell for each request's lat/lon, then join that cell to the
index set of city-hex-polygons-8.geojson. Requests with no coordinates get index "0".
Requests with coordinates that cannot be joined to the City grid (invalid coordinates, or
a cell outside the grid) also get "0" and count towards the join error rate.

Exit codes: 0 ok, 3 output differs unexpectedly from sr_hex.csv.gz, 4 join error rate
above the threshold.
"""

import argparse
import json
import logging
import re
import sys
import time
import urllib.request
from dataclasses import dataclass
from pathlib import Path

import h3
import pandas as pd

from timing import timed

log = logging.getLogger("sr_join")

BASE_URL = "https://cct-ds-code-challenge-input-data.s3.af-south-1.amazonaws.com/"
SR_FILE = "sr.csv.gz"
REFERENCE_FILE = "sr_hex.csv.gz"
HEX_FILE = "city-hex-polygons-8.geojson"
DEFAULT_DATA_DIR = Path("data")
DEFAULT_OUT = Path("output") / "sr_hex_joined.csv.gz"
RESOLUTION = 8
NO_INDEX = "0"
INDEX_COLUMN = "h3_level8_index"
KEY_COLUMN = "notification_number"

# Observed on the full year: 3 of 729,270 located requests (0.0004%) fall outside the
# City grid. 0.1% allows ~700 such rows (about 250x the observed rate): bad geocodes at
# the edge of the City are expected and immaterial to per-hex counts, but a rate near
# 0.1% would mean a grid/projection/column mix-up that would corrupt the analysis.
DEFAULT_MAX_JOIN_ERROR_RATE = 0.001


class JoinThresholdError(RuntimeError):
    """Raised when the join error rate exceeds the configured threshold."""


@dataclass
class JoinStats:
    total: int
    missing_coords: int
    partial_coords: int
    invalid_coords: int
    outside_grid: int

    @property
    def located(self) -> int:
        return self.total - self.missing_coords

    @property
    def failed(self) -> int:
        return self.invalid_coords + self.outside_grid

    @property
    def joined(self) -> int:
        return self.located - self.failed

    @property
    def error_rate(self) -> float:
        return self.failed / self.located if self.located else 0.0


def download(url: str, dest: Path, retries: int = 4, timeout: int = 60) -> Path:
    """Download to dest with retry and backoff; reuse the file if it already exists."""
    if dest.exists():
        log.info("using cached %s", dest)
        return dest
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".part")
    for attempt in range(1, retries + 1):
        try:
            with timed(log, f"download {url.rsplit('/', 1)[-1]}"):
                with urllib.request.urlopen(url, timeout=timeout) as resp, open(tmp, "wb") as fh:
                    while chunk := resp.read(1 << 20):
                        fh.write(chunk)
            tmp.replace(dest)
            return dest
        except OSError as exc:
            if attempt == retries:
                raise
            wait = 2**attempt
            log.warning("download failed (%s), retry %d/%d in %ds", exc, attempt, retries, wait)
            time.sleep(wait)
    raise AssertionError("unreachable")


def load_hex_index(geojson_path: Path) -> set[str]:
    with open(geojson_path, encoding="utf-8") as fh:
        collection = json.load(fh)
    indexes = [f["properties"]["index"] for f in collection["features"]]
    if len(indexes) != len(set(indexes)):
        raise ValueError(f"{geojson_path} contains duplicate hex indexes")
    return set(indexes)


def find_column(df: pd.DataFrame, name: str) -> str:
    """Case-insensitive column lookup (the README says Latitude, the data says latitude)."""
    for col in df.columns:
        if col.lower() == name.lower():
            return col
    raise KeyError(f"column {name!r} not found in {list(df.columns)}")


def assign_hex_index(
    lat: pd.Series, lon: pd.Series, hexes: set[str]
) -> tuple[pd.Series, JoinStats]:
    """Return (H3 index per row as str, stats). Unjoinable rows get NO_INDEX."""
    lat = pd.to_numeric(lat, errors="coerce")
    lon = pd.to_numeric(lon, errors="coerce")
    missing = lat.isna() | lon.isna()
    partial = lat.isna() ^ lon.isna()
    valid_range = lat.between(-90, 90) & lon.between(-180, 180)
    invalid = ~missing & ~valid_range
    candidate = ~missing & valid_range

    index = pd.Series(NO_INDEX, index=lat.index, dtype=object)
    cells = pd.Series(
        [
            h3.latlng_to_cell(la, lo, RESOLUTION)
            for la, lo in zip(lat[candidate], lon[candidate], strict=True)
        ],
        index=lat.index[candidate],
        dtype=object,
    )
    in_grid = cells.isin(hexes)
    index.loc[cells.index[in_grid]] = cells[in_grid]

    stats = JoinStats(
        total=len(lat),
        missing_coords=int(missing.sum()),
        partial_coords=int(partial.sum()),
        invalid_coords=int(invalid.sum()),
        outside_grid=int((~in_grid).sum()),
    )
    return index, stats


def drop_export_artifacts(df: pd.DataFrame) -> pd.DataFrame:
    """Drop leaked pandas row-index columns ('Unnamed: 0') that sr.csv.gz was exported with."""
    artifacts = [c for c in df.columns if re.fullmatch(r"Unnamed: \d+", str(c))]
    if artifacts:
        log.info("dropping export artifact column(s): %s", artifacts)
    return df.drop(columns=artifacts)


def enforce_threshold(stats: JoinStats, max_rate: float) -> None:
    log.info(
        "join summary: total=%d no_coords=%d (partial=%d) with_coords=%d joined=%d "
        "failed=%d (invalid=%d, outside_grid=%d) error_rate=%.6f%% threshold=%.4f%%",
        stats.total,
        stats.missing_coords,
        stats.partial_coords,
        stats.located,
        stats.joined,
        stats.failed,
        stats.invalid_coords,
        stats.outside_grid,
        100 * stats.error_rate,
        100 * max_rate,
    )
    if stats.error_rate > max_rate:
        raise JoinThresholdError(
            f"join error rate {stats.error_rate:.6%} exceeds threshold {max_rate:.4%} "
            f"({stats.failed} of {stats.located} located requests failed to join)"
        )


def compare_to_reference(joined: pd.DataFrame, reference: pd.DataFrame, hexes: set[str]) -> dict:
    """Compare our index to sr_hex.csv.gz by notification number.

    Differences are 'expected' only where the reference holds a real H3 cell that is not in
    the City grid (we deliberately output 0 there). Anything else is unexpected.
    """
    for name, df in (("output", joined), ("reference", reference)):
        if df[KEY_COLUMN].duplicated().any():
            raise ValueError(f"{name} has duplicate {KEY_COLUMN} values")
    ours = joined.set_index(KEY_COLUMN)[INDEX_COLUMN].astype(str)
    theirs = reference.set_index(KEY_COLUMN)[INDEX_COLUMN].astype(str)
    only_ours = ours.index.difference(theirs.index)
    only_theirs = theirs.index.difference(ours.index)
    both = ours.index.intersection(theirs.index)
    diff = both[ours[both].values != theirs[both].values]
    expected = [
        k for k in diff if ours[k] == NO_INDEX and theirs[k] not in hexes and theirs[k] != NO_INDEX
    ]
    unexpected = [k for k in diff if k not in set(expected)]
    return {
        "compared": len(both),
        "only_in_output": len(only_ours),
        "only_in_reference": len(only_theirs),
        "expected_differences": expected,
        "unexpected_differences": unexpected,
        "matches": not (len(only_ours) or len(only_theirs) or unexpected),
    }


def join_service_requests(sr: pd.DataFrame, hexes: set[str], max_rate: float) -> pd.DataFrame:
    lat_col, lon_col = find_column(sr, "latitude"), find_column(sr, "longitude")
    if INDEX_COLUMN in sr.columns:
        raise ValueError(f"input already has a {INDEX_COLUMN} column")
    with timed(log, f"assign H3 res {RESOLUTION} index to {len(sr)} requests"):
        index, stats = assign_hex_index(sr[lat_col], sr[lon_col], hexes)
    enforce_threshold(stats, max_rate)
    out = drop_export_artifacts(sr)
    out[INDEX_COLUMN] = index
    if len(out) != len(sr):
        raise AssertionError("row count changed during join")
    return out


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, default=DEFAULT_DATA_DIR)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    parser.add_argument("--max-join-error-rate", type=float, default=DEFAULT_MAX_JOIN_ERROR_RATE)
    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)-7s %(name)s: %(message)s"
    )

    with timed(log, "total pipeline"):
        sr_path = download(BASE_URL + SR_FILE, args.data_dir / SR_FILE)
        hex_path = download(BASE_URL + HEX_FILE, args.data_dir / HEX_FILE)
        ref_path = download(BASE_URL + REFERENCE_FILE, args.data_dir / REFERENCE_FILE)

        hexes = load_hex_index(hex_path)
        log.info("loaded %d hexagons from %s", len(hexes), HEX_FILE)
        # Everything is read as text with no NA conversion so the output is byte-faithful to
        # the input: pandas type inference would strip leading zeros from notification_number,
        # add ".0" to reference_number and turn the literal "None" in cause_code into blank.
        # Coordinates are parsed to numbers separately inside assign_hex_index.
        with timed(log, "read sr.csv.gz"):
            sr = pd.read_csv(sr_path, dtype=str, keep_default_na=False)

        try:
            joined = join_service_requests(sr, hexes, args.max_join_error_rate)
        except JoinThresholdError as exc:
            log.error("%s", exc)
            return 4

        with timed(log, "read sr_hex.csv.gz (reference)"):
            reference = pd.read_csv(
                ref_path, usecols=[KEY_COLUMN, INDEX_COLUMN], dtype=str, keep_default_na=False
            )
        result = compare_to_reference(joined, reference, hexes)
        log.info(
            "reference check: compared=%d expected_differences=%d unexpected_differences=%d "
            "only_in_output=%d only_in_reference=%d",
            result["compared"],
            len(result["expected_differences"]),
            len(result["unexpected_differences"]),
            result["only_in_output"],
            result["only_in_reference"],
        )
        if result["expected_differences"]:
            log.info(
                "expected differences (reference cell is outside the City grid): %s",
                result["expected_differences"],
            )
        if not result["matches"]:
            log.error("output differs unexpectedly from %s", REFERENCE_FILE)
            return 3

        args.out.parent.mkdir(parents=True, exist_ok=True)
        with timed(log, f"write {args.out}"):
            joined.to_csv(args.out, index=False, compression={"method": "gzip", "compresslevel": 1})
    log.info("done")
    return 0


if __name__ == "__main__":
    sys.exit(main())
