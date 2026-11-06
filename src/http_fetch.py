"""Resilient HTTP fetch for unreliable public endpoints.

Strategy (and why):
  1. Retry each URL with exponential backoff plus jitter. Failures on government open-data
     endpoints are usually transient (timeouts, 5xx, throttling), and jitter avoids
     hammering a struggling server in lock step with other clients.
  2. Try mirror URLs in order, so one dead host does not block the run.
  3. Validate the payload before trusting it. An error page returned with HTTP 200 or a
     truncated download must not be cached or parsed as data.
  4. Cache the last good copy on disk (atomic write). If every attempt fails, fall back to
     that copy with a loud warning, because stale reference data is better than no run for
     a dataset that does not change (a closed calendar year of wind readings).
  5. With no network and no cache, fail with a clear error instead of producing output
     that silently lacks the data.
"""
import http.client
import logging
import os
import random
import time
import urllib.request
from pathlib import Path
from typing import Callable, Iterable

log = logging.getLogger(__name__)

USER_AGENT = "ds-code-challenge/1.0"


class FetchError(RuntimeError):
    """Raised when a resource cannot be fetched and no usable cached copy exists."""


def _default_opener(url: str, timeout: float) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


def fetch_bytes(
    urls: Iterable[str],
    cache_path: Path,
    validate: Callable[[bytes], None],
    retries: int = 4,
    base_delay: float = 2.0,
    max_delay: float = 60.0,
    timeout: float = 60.0,
    opener: Callable[[str, float], bytes] = _default_opener,
    sleep: Callable[[float], None] = time.sleep,
    rng: random.Random | None = None,
) -> tuple[bytes, str]:
    """Return (payload, source) where source is the URL used or 'cache'.

    `validate` must raise ValueError if the payload is not acceptable.
    """
    rng = rng or random.Random()
    cache_path = Path(cache_path)
    errors: list[str] = []
    for url in urls:
        for attempt in range(1, retries + 1):
            try:
                payload = opener(url, timeout)
                validate(payload)
            # HTTPException covers a connection dropped mid-download (IncompleteRead), which
            # is not an OSError.
            except (OSError, http.client.HTTPException, ValueError) as exc:
                errors.append(f"{url} attempt {attempt}: {exc}")
                if attempt == retries:
                    log.warning("giving up on %s after %d attempts (%s)", url, retries, exc)
                    break
                delay = min(max_delay, base_delay * 2 ** (attempt - 1)) * rng.uniform(0.5, 1.0)
                log.warning("fetch failed (%s), retry %d/%d in %.1fs", exc, attempt, retries - 1, delay)
                sleep(delay)
                continue
            _write_atomic(cache_path, payload)
            log.info("fetched %s (%d bytes), cached to %s", url, len(payload), cache_path)
            return payload, url

    if cache_path.exists():
        payload = cache_path.read_bytes()
        try:
            validate(payload)
        except ValueError as exc:
            raise FetchError(f"all sources failed and cached copy {cache_path} is invalid: {exc}") from exc
        age_days = (time.time() - cache_path.stat().st_mtime) / 86400
        log.warning(
            "ALL SOURCES FAILED, falling back to cached copy %s (%.1f days old). Last errors: %s",
            cache_path, age_days, errors[-1] if errors else "none",
        )
        return payload, "cache"

    raise FetchError(
        f"could not fetch resource and no cache at {cache_path}. Last errors: "
        + "; ".join(errors[-3:])
    )


def _write_atomic(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + f".{os.getpid()}.part")
    tmp.write_bytes(payload)
    tmp.replace(path)
