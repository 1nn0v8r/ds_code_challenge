"""Task 5.3: anonymise the augmented Atlantis subsample.

Required precision kept: location to about 500 m (H3 res 8 hexagon, ~0.53 km average edge) and
time to 6 hours (creation and completion floored to 00/06/12/18 local time).

What is removed or coarsened, and why:
  * latitude, longitude, distance_to_centroid_m: exact position; the hexagon replaces them.
    The distance column would let a reader triangulate back toward a point.
  * official_suburb: most hexagons here span more than one suburb, so the suburb would place
    a request more finely than the required precision.
  * notification_number, reference_number: these link a row to the City's billing and
    service systems, where the resident's name and address live. Replaced by a random
    surrogate id (shuffled order, no relation to the originals, no mapping kept).
  * creation_timestamp, completion_timestamp: floored to 6-hour buckets.
  * wind_timestamp: the matched reading's exact time would narrow the request time below
    6 hours, so it is dropped.
  * wind_speed, wind_direction: the wind file is public, so an exact reading can be looked up
    to recover the time it was taken, undoing the 6-hour bucket. They are replaced by the
    mean over the whole bucket, which is the same for every request in that bucket and so
    reveals nothing about when within it a request was made.
  * branch, section: internal org units that add no analytical value here but narrow a
    request to a small team's patch. Dropped.

Records held back for a person to anonymise by hand (written to a separate restricted
file, removed from the anonymised output):
  * RARE_VALUE: a category value (such as the request code or its cause) occurs fewer than
    `rare_value_min` times, so that value alone can identify a resident (e.g. a single 'Bees
    in Water Meter').
  * SENSITIVE_SMALL_CLASS: the request is in a sensitive area (billing, valuations, rates,
    housing tenancy, health, meter and revenue protection queries, which describe a
    household's finances, tenure or health) AND fewer than `k_sensitive` records share its
    hexagon, 6-hour bucket and request group, so it can be singled out by someone who
    knows when and where a neighbour contacted the City.
Holding records back can make other values rare, so both rules repeat until nothing changes.

Not claimed: for non-sensitive records the hexagon plus 6-hour bucket is a small class, and
many rows are unique on those two alone. That precision is required by the brief, so the
residual risk is documented in docs/task5_atlantis.md rather than hidden.
"""
import logging
import secrets
from dataclasses import dataclass

import numpy as np
import pandas as pd

from timing import timed

log = logging.getLogger(__name__)

BUCKET = "6h"
LOCAL_TZ = "Africa/Johannesburg"

DROP_COLUMNS = [
    "latitude", "longitude", "distance_to_centroid_m", "official_suburb",
    "notification_number", "reference_number",
    "wind_timestamp", "wind_speed", "wind_direction", "branch", "section",
]
TIME_COLUMNS = ["creation_timestamp", "completion_timestamp"]
RARE_COLUMNS = ["directorate", "department", "code_group", "code", "cause_code_group", "cause_code"]
WIND_SPEED_MEAN = "wind_speed_6h_mean"
WIND_DIRECTION_MEAN = "wind_direction_6h_mean"

SENSITIVE_DEPARTMENTS = {
    "Revenue", "Valuations", "City Health", "Customer Relations",
    "Home Ownership Transfer, Tenancy Management and Staff Housing",
}
SENSITIVE_CODE_GROUPS = {
    "REVENUE", "HOUSING - EXISTING", "HEALTH", "VALUATIONS",
    "ELECTRICITY FINANCIAL AND METER READING", "ELECTRICITY INTERNAL(REVENUE PROTECTION)",
    "WATER AND SANITATION OR METER QUERIES", "PROPERTY MANAGEMENT",
}


@dataclass
class AnonymisationResult:
    anonymised: pd.DataFrame
    held_for_review: pd.DataFrame
    summary: dict


def floor_to_bucket(series: pd.Series, bucket: str = BUCKET) -> pd.Series:
    """Floor timestamps to a bucket in local time. Blank or unparseable values become empty."""
    ts = pd.to_datetime(series.replace("", np.nan), utc=True, errors="coerce").dt.tz_convert(LOCAL_TZ)
    floored = ts.dt.floor(bucket)
    return floored.dt.strftime("%Y-%m-%d %H:%M").fillna("")


def is_sensitive(df: pd.DataFrame) -> pd.Series:
    return df["department"].isin(SENSITIVE_DEPARTMENTS) | df["code_group"].isin(SENSITIVE_CODE_GROUPS)


def summarise_wind_by_bucket(wind: pd.DataFrame) -> pd.DataFrame:
    """Mean wind per 6-hour bucket, indexed by bucket label.

    Direction is a circular mean, so 350 and 10 degrees average to 0 rather than 180.
    """
    radians = np.radians(pd.to_numeric(wind["wind_direction"], errors="coerce").to_numpy(dtype=float))
    frame = pd.DataFrame({
        "bucket": floor_to_bucket(wind["wind_timestamp"]).to_numpy(),
        "speed": pd.to_numeric(wind["wind_speed"], errors="coerce").to_numpy(dtype=float),
        "sin": np.sin(radians),
        "cos": np.cos(radians),
    })
    means = frame[frame["bucket"] != ""].groupby("bucket").mean()
    direction = np.degrees(np.arctan2(means["sin"], means["cos"])) % 360
    return pd.DataFrame({
        WIND_SPEED_MEAN: means["speed"].round(1),
        WIND_DIRECTION_MEAN: direction.round(0) % 360,
    })


def _review_reasons(work: pd.DataFrame, rare_value_min: int, k_sensitive: int) -> pd.Series:
    """Reason each record is held for review, or "" if it is not."""
    reason = pd.Series("", index=work.index, dtype=object)
    columns = [c for c in RARE_COLUMNS if c in work.columns]
    while True:
        kept = work.loc[reason == ""]
        rare = pd.Series(False, index=kept.index)
        for col in columns:
            counts = kept[col].map(kept[col].value_counts())
            rare |= (counts < rare_value_min) & (kept[col] != "")
        class_size = kept.groupby(["h3_level8_index", "_bucket", "code_group"])["code"].transform("size")
        small = is_sensitive(kept) & (class_size < k_sensitive)
        if not (rare | small).any():
            return reason
        reason.loc[small[small].index] = "SENSITIVE_SMALL_CLASS"
        reason.loc[rare[rare].index] = "RARE_VALUE"


def anonymise(
    df: pd.DataFrame,
    rare_value_min: int = 5,
    k_sensitive: int = 3,
    seed: int | None = None,
    wind: pd.DataFrame | None = None,
) -> AnonymisationResult:
    """Return the anonymised table plus the records held for manual review.

    `wind` is the full series of readings, used for the per-bucket means. Without it, any
    exact wind columns are still removed and no wind columns are output.

    `seed` exists only for reproducible tests; in production leave it None so the surrogate
    ids come from OS entropy and cannot be reproduced.
    """
    required = {"latitude", "longitude", "h3_level8_index", "creation_timestamp",
                "completion_timestamp", "code", "code_group", "department"}
    missing = required - set(df.columns)
    if missing:
        raise KeyError(f"missing columns: {sorted(missing)}")
    if df["h3_level8_index"].isin(["", "0"]).any():
        raise ValueError("rows without a valid H3 index cannot be anonymised to hex precision")

    with timed(log, f"anonymise {len(df)} records"):
        work = df.copy()
        work["_bucket"] = floor_to_bucket(work["creation_timestamp"])
        if (work["_bucket"] == "").any():
            raise ValueError("rows with unparseable creation_timestamp cannot be bucketed")

        reason = _review_reasons(work, rare_value_min, k_sensitive)
        held_mask = reason != ""

        held = df.loc[held_mask].copy()
        held.insert(0, "review_reason", reason[held_mask])

        keep = work.loc[~held_mask].copy()
        for col in TIME_COLUMNS:
            keep[col] = floor_to_bucket(keep[col])
        if wind is not None:
            means = summarise_wind_by_bucket(wind)
            for col in means.columns:
                keep[col] = keep["_bucket"].map(means[col])
        elif "wind_speed" in keep.columns:
            log.warning("no wind readings given: exact wind columns removed and no means added")
        keep = keep.drop(columns=[c for c in DROP_COLUMNS if c in keep.columns] + ["_bucket"])

        rng = np.random.default_rng(seed if seed is not None else secrets.randbits(128))
        ids = rng.permutation(len(keep))
        keep.insert(0, "record_id", [f"R{i:06d}" for i in ids])
        keep = keep.sort_values("record_id").reset_index(drop=True)

    summary = {
        "input_records": len(df),
        "anonymised_records": len(keep),
        "held_for_review": len(held),
        "held_rare_value": int((reason == "RARE_VALUE").sum()),
        "held_sensitive_small_class": int((reason == "SENSITIVE_SMALL_CLASS").sum()),
        "dropped_columns": [c for c in DROP_COLUMNS if c in df.columns],
    }
    log.info(
        "anonymised %d of %d records; %d held for manual review (%d rare value, %d sensitive small class)",
        summary["anonymised_records"], summary["input_records"], summary["held_for_review"],
        summary["held_rare_value"], summary["held_sensitive_small_class"],
    )
    return AnonymisationResult(keep, held, summary)


def check_precision(original: pd.DataFrame, anonymised: pd.DataFrame) -> dict:
    """Self-check on the output: no precision finer than required has leaked through."""
    problems = []
    for col in DROP_COLUMNS:
        if col in anonymised.columns:
            problems.append(f"column {col} still present")
    for col in TIME_COLUMNS:
        stamps = pd.to_datetime(anonymised[col].replace("", np.nan), errors="coerce").dropna()
        if len(stamps) and not (
            (stamps.dt.hour % 6 == 0) & (stamps.dt.minute == 0) & (stamps.dt.second == 0)
        ).all():
            problems.append(f"{col} not on a 6-hour boundary")
    for col in (WIND_SPEED_MEAN, WIND_DIRECTION_MEAN):
        if col in anonymised.columns:
            if (anonymised.groupby("creation_timestamp")[col].nunique() > 1).any():
                problems.append(f"{col} varies within a 6-hour bucket")
    if anonymised["record_id"].duplicated().any():
        problems.append("duplicate record_id")
    originals = set(original.get("notification_number", pd.Series(dtype=str)).astype(str))
    if originals & set(anonymised["record_id"].astype(str)):
        problems.append("record_id equals an original identifier")
    return {"ok": not problems, "problems": problems}
