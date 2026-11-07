"""Task 5.1: subsample service requests within 1 arc-minute of the Atlantis centroid.

Suburb choice: ATLANTIS, taken from the "Major suburbs" layer of the Western Cape GIS server.
The "Official Planning Suburbs" layers on that server list only ATLANTIS INDUSTRIAL under
that name, which is an industrial area rather than the suburb of Atlantis, so they are not
used.

Centroid method (computed, not hard coded):
  1. Download the ATLANTIS polygon from that layer (ArcGIS REST query, GeoJSON, WGS84).
  2. Project to UTM zone 34S (EPSG:32734, metres, suits the Cape) and take the polygon's
     area-weighted centroid there, then convert back to lat/lon. A centroid taken directly
     in degrees distorts east-west distances; for this polygon the difference is under
     1 m, so it is immaterial here but the projected method stays correct elsewhere.

Distance rule: "within 1 minute" is read as within 1 arc-minute of the centroid, taken as a
geodesic distance of at most 1852 m (1 nautical mile) on the WGS84 ellipsoid. That is a
convention: a true arc-minute of latitude is about 1849 m here. A circle is used rather than
a lat/lon box because a minute of longitude is only about 1.54 km at this latitude, which
would make a box lopsided.
"""

import json
import logging
import urllib.parse
from pathlib import Path

import numpy as np
import pandas as pd
import shapely
from pyproj import Geod, Transformer
from shapely.geometry import shape

from http_fetch import fetch_bytes
from timing import timed

log = logging.getLogger(__name__)

SUBURB_LAYER_URL = (
    "https://gis.westerncape.gov.za/server2/rest/services/SpatialDataWarehouse/"
    "CoCT_Management_Boundaries/MapServer/15/query"
)
SUBURB_NAME_FIELD = "SBRB_NAME"
SUBURB_NAME = "ATLANTIS"
METRIC_CRS = "EPSG:32734"
ARC_MINUTE_M = 1852.0
GEOD = Geod(ellps="WGS84")


def suburb_query_url(name: str = SUBURB_NAME) -> str:
    params = {
        "where": f"UPPER({SUBURB_NAME_FIELD})='{name.upper()}'",
        "outFields": f"OBJECTID,{SUBURB_NAME_FIELD}",
        "returnGeometry": "true",
        "outSR": "4326",
        "f": "geojson",
    }
    return SUBURB_LAYER_URL + "?" + urllib.parse.urlencode(params)


def validate_suburb_geojson(payload: bytes) -> None:
    try:
        data = json.loads(payload)
    except json.JSONDecodeError as exc:
        raise ValueError(f"not JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise ValueError(f"expected a JSON object, got {type(data).__name__}")
    if "error" in data:
        raise ValueError(f"server error: {data['error']}")
    features = data.get("features") or []
    if len(features) != 1:
        raise ValueError(f"expected exactly 1 suburb polygon, got {len(features)}")
    feature = features[0] if isinstance(features[0], dict) else {}
    # The name check also stops a cached copy of a different suburb being reused as a fallback.
    name = (feature.get("properties") or {}).get(SUBURB_NAME_FIELD)
    if name != SUBURB_NAME:
        raise ValueError(f"expected suburb {SUBURB_NAME!r}, got {name!r}")
    geometry = feature.get("geometry")
    if not isinstance(geometry, dict) or geometry.get("type") not in ("Polygon", "MultiPolygon"):
        raise ValueError("suburb geometry is not a polygon")


def compute_centroid(geometry: dict) -> tuple[float, float]:
    """Return (lat, lon) of the polygon centroid, computed in a metric projection."""
    poly = shape(geometry)
    if not poly.is_valid:
        poly = poly.buffer(0)
    to_metric = Transformer.from_crs("EPSG:4326", METRIC_CRS, always_xy=True).transform
    to_wgs = Transformer.from_crs(METRIC_CRS, "EPSG:4326", always_xy=True).transform
    c = shapely.transform(poly, to_metric, interleaved=False).centroid
    lon, lat = to_wgs(c.x, c.y)
    return lat, lon


def get_atlantis_centroid(cache_dir: Path) -> tuple[float, float]:
    payload, source = fetch_bytes(
        [suburb_query_url()], Path(cache_dir) / "atlantis_suburb.geojson", validate_suburb_geojson
    )
    geometry = json.loads(payload)["features"][0]["geometry"]
    lat, lon = compute_centroid(geometry)
    log.info("%s centroid (from %s): lat=%.6f lon=%.6f", SUBURB_NAME, source, lat, lon)
    return lat, lon


def distance_m(lat: pd.Series, lon: pd.Series, centre: tuple[float, float]) -> np.ndarray:
    """Geodesic distance in metres from each point to centre. NaN where coordinates are missing."""
    lat = pd.to_numeric(lat, errors="coerce").to_numpy(dtype=float)
    lon = pd.to_numeric(lon, errors="coerce").to_numpy(dtype=float)
    out = np.full(len(lat), np.nan)
    ok = np.isfinite(lat) & np.isfinite(lon) & (np.abs(lat) <= 90) & (np.abs(lon) <= 180)
    if ok.any():
        _, _, dist = GEOD.inv(
            lon[ok], lat[ok], np.full(ok.sum(), centre[1]), np.full(ok.sum(), centre[0])
        )
        out[ok] = dist
    return out


def subsample(
    sr: pd.DataFrame, centre: tuple[float, float], radius_m: float = ARC_MINUTE_M
) -> pd.DataFrame:
    """Rows within radius_m of centre. Requests without usable coordinates are excluded."""
    with timed(log, f"distance filter on {len(sr)} requests"):
        dist = distance_m(sr["latitude"], sr["longitude"], centre)
    mask = dist <= radius_m
    out = sr.loc[mask].copy()
    out["distance_to_centroid_m"] = np.round(dist[mask], 1)
    log.info(
        "subsample: %d of %d requests within %.0f m of centroid (%d had no usable coordinates)",
        len(out),
        len(sr),
        radius_m,
        int(np.isnan(dist).sum()),
    )
    return out
