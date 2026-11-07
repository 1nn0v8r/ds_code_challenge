"""Tests for http_fetch. Nothing here leaves the machine: most tests script the opener, and
the real opener is exercised against a throwaway HTTP server on localhost."""

import http.server
import logging
import os
import random
import threading
import urllib.error

import pytest

from http_fetch import USER_AGENT, FetchError, _default_opener, fetch_bytes

GOOD = b"GOOD payload"
URL_A = "https://a.example/file"
URL_B = "https://b.example/file"


def validate(payload: bytes) -> None:
    if not payload.startswith(b"GOOD"):
        raise ValueError("not a good payload")


class ScriptedOpener:
    """Plays back a list of results per URL. An exception in the list is raised."""

    def __init__(self):
        self.script = {}
        self.calls = []

    def on(self, url, *results):
        self.script[url] = list(results)
        return self

    def __call__(self, url, timeout):
        self.calls.append(url)
        result = self.script[url].pop(0)
        if isinstance(result, Exception):
            raise result
        return result


@pytest.fixture
def opener():
    return ScriptedOpener()


@pytest.fixture
def sleeps():
    return []


@pytest.fixture
def fetch(tmp_path, opener, sleeps):
    cache = tmp_path / "cache" / "file.bin"

    def _fetch(urls, **kwargs):
        return fetch_bytes(urls, cache, validate, opener=opener, sleep=sleeps.append, **kwargs)

    _fetch.cache = cache
    return _fetch


def test_first_try_success_returns_payload_and_caches_it(fetch, opener, sleeps):
    opener.on(URL_A, GOOD)
    assert fetch([URL_A]) == (GOOD, URL_A)
    assert fetch.cache.read_bytes() == GOOD
    assert sleeps == []


def test_transient_failure_is_retried_on_the_same_url(fetch, opener, sleeps):
    opener.on(URL_A, TimeoutError("slow"), GOOD)
    assert fetch([URL_A]) == (GOOD, URL_A)
    assert opener.calls == [URL_A, URL_A]
    assert len(sleeps) == 1


def test_http_error_status_is_retried(fetch, opener):
    error = urllib.error.HTTPError(URL_A, 503, "Unavailable", {}, None)
    opener.on(URL_A, error, GOOD)
    assert fetch([URL_A]) == (GOOD, URL_A)


def test_retries_stop_at_the_limit_and_do_not_sleep_after_the_last_attempt(fetch, opener, sleeps):
    opener.on(URL_A, *[OSError("down")] * 3)
    with pytest.raises(FetchError):
        fetch([URL_A], retries=3)
    assert len(opener.calls) == 3
    assert len(sleeps) == 2


def test_backoff_doubles_with_jitter_and_is_capped(fetch, opener, sleeps):
    opener.on(URL_A, *[OSError("down")] * 5)
    with pytest.raises(FetchError):
        fetch([URL_A], retries=5, base_delay=2.0, max_delay=5.0, rng=random.Random(1))
    # Uncapped delays would be 2, 4, 8, 16. Jitter multiplies each by 0.5 to 1.0.
    for delay, ceiling in zip(sleeps, [2.0, 4.0, 5.0, 5.0], strict=True):
        assert ceiling * 0.5 <= delay <= ceiling


def test_mirror_is_tried_after_the_first_url_is_exhausted(fetch, opener):
    opener.on(URL_A, OSError("down"), OSError("down")).on(URL_B, GOOD)
    assert fetch([URL_A, URL_B], retries=2) == (GOOD, URL_B)
    assert opener.calls == [URL_A, URL_A, URL_B]


def test_invalid_payload_is_retried_and_never_cached(fetch, opener):
    opener.on(URL_A, b"<html>error page</html>", b"<html>error page</html>")
    with pytest.raises(FetchError):
        fetch([URL_A], retries=2)
    assert not fetch.cache.exists()


def test_invalid_payload_does_not_overwrite_a_good_cache(fetch, opener):
    fetch.cache.parent.mkdir(parents=True)
    fetch.cache.write_bytes(GOOD + b" old")
    opener.on(URL_A, b"<html>error page</html>")
    assert fetch([URL_A], retries=1) == (GOOD + b" old", "cache")
    assert fetch.cache.read_bytes() == GOOD + b" old"


def test_all_sources_failing_falls_back_to_cache_with_a_warning(fetch, opener, caplog):
    fetch.cache.parent.mkdir(parents=True)
    fetch.cache.write_bytes(GOOD)
    opener.on(URL_A, OSError("down"))
    with caplog.at_level(logging.WARNING, logger="http_fetch"):
        assert fetch([URL_A], retries=1) == (GOOD, "cache")
    assert "ALL SOURCES FAILED" in caplog.text


def test_network_is_preferred_over_cache_and_refreshes_it(fetch, opener):
    fetch.cache.parent.mkdir(parents=True)
    fetch.cache.write_bytes(GOOD + b" old")
    opener.on(URL_A, GOOD + b" new")
    assert fetch([URL_A]) == (GOOD + b" new", URL_A)
    assert fetch.cache.read_bytes() == GOOD + b" new"


def test_corrupt_cache_raises_instead_of_being_returned(fetch, opener):
    fetch.cache.parent.mkdir(parents=True)
    fetch.cache.write_bytes(b"garbage")
    opener.on(URL_A, OSError("down"))
    with pytest.raises(FetchError, match="invalid"):
        fetch([URL_A], retries=1)


def test_no_network_and_no_cache_raises_with_the_last_error(fetch, opener):
    opener.on(URL_A, OSError("connection refused"))
    with pytest.raises(FetchError, match="connection refused"):
        fetch([URL_A], retries=1)


def test_no_temporary_file_is_left_behind(fetch, opener):
    opener.on(URL_A, GOOD)
    fetch([URL_A])
    assert os.listdir(fetch.cache.parent) == [fetch.cache.name]


def test_programming_errors_are_not_swallowed_as_network_failures(fetch, opener):
    opener.on(URL_A, TypeError("bug"))
    with pytest.raises(TypeError):
        fetch([URL_A])


# The real opener, against a local server, so real urllib behaviour is what gets tested.


class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        self.server.seen_user_agents.append(self.headers.get("User-Agent"))
        behaviour = self.server.behaviours.pop(0)
        if behaviour == "ok":
            self.send_response(200)
            self.send_header("Content-Length", str(len(GOOD)))
            self.end_headers()
            self.wfile.write(GOOD)
        elif behaviour == "500":
            self.send_error(500)
        elif behaviour == "truncated":
            # Promise more bytes than are sent, then hang up mid-download.
            self.send_response(200)
            self.send_header("Content-Length", str(len(GOOD) + 100))
            self.end_headers()
            self.wfile.write(GOOD)
            self.close_connection = True

    def log_message(self, *args):
        pass


@pytest.fixture
def server():
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    srv.behaviours = []
    srv.seen_user_agents = []
    srv.url = f"http://127.0.0.1:{srv.server_address[1]}/file"
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    yield srv
    srv.shutdown()
    srv.server_close()


def real_fetch(server, tmp_path, retries=2):
    return fetch_bytes(
        [server.url],
        tmp_path / "file.bin",
        validate,
        retries=retries,
        opener=_default_opener,
        sleep=lambda _: None,
        timeout=5,
    )


def test_real_opener_downloads_and_sends_the_user_agent(server, tmp_path):
    server.behaviours = ["ok"]
    assert real_fetch(server, tmp_path) == (GOOD, server.url)
    assert server.seen_user_agents == [USER_AGENT]


def test_real_opener_retries_a_server_error(server, tmp_path):
    server.behaviours = ["500", "ok"]
    assert real_fetch(server, tmp_path) == (GOOD, server.url)


def test_real_opener_retries_a_truncated_download_and_never_caches_it(server, tmp_path):
    server.behaviours = ["truncated", "ok"]
    assert real_fetch(server, tmp_path) == (GOOD, server.url)
    assert (tmp_path / "file.bin").read_bytes() == GOOD


def test_real_opener_truncated_every_time_raises_fetch_error(server, tmp_path):
    server.behaviours = ["truncated", "truncated"]
    with pytest.raises(FetchError):
        real_fetch(server, tmp_path)
    assert not (tmp_path / "file.bin").exists()
