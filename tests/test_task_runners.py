"""End-to-end tests of the three task entry points, with no network.

Each test runs a task's real main() over real files. Only the outside world is replaced: the
S3 client, and the suburb and wind downloads. Inputs that are "already cached" in the data
folder skip the download, exactly as a repeat run does.
"""

import gzip
import http.server
import io
import json
import threading

import h3
import pandas as pd
import pytest
from test_extraction import make_collection, make_feature
from test_wind import REAL_LAYOUT, make_ods

import atlantis_subsample
import aws_client
import run_atlantis
import run_extraction
import sr_join
import wind
from atlantis_subsample import SUBURB_NAME, SUBURB_NAME_FIELD
from extract import parse_collection
from http_fetch import FetchError

# Task 1


def feature_line(feature):
    return json.dumps(feature).replace('{"type": "Feature"', '{ "type": "Feature"', 1) + ","


def select_payload(collection):
    lines = [
        f'"type": "{collection["type"]}",',
        '"crs": ' + json.dumps(collection["crs"]) + ",",
        *[feature_line(f) for f in collection["features"]],
    ]
    return "\n".join(lines).encode()


class FakeS3:
    def __init__(self, selected, reference):
        self.selected = selected
        self.reference = reference
        self.select_calls = []

    def select_object_content(self, **kwargs):
        self.select_calls.append(kwargs)
        stats = {"Details": {"BytesScanned": 1000, "BytesReturned": len(self.selected)}}
        # Split the payload, because S3 streams results in several events.
        middle = len(self.selected) // 2
        return {
            "Payload": [
                {"Records": {"Payload": self.selected[:middle]}},
                {"Records": {"Payload": self.selected[middle:]}},
                {"Stats": stats},
                {"End": {}},
            ]
        }

    def get_object(self, Bucket, Key):  # noqa: N803 (the boto3 argument names)
        assert Key == run_extraction.REFERENCE_KEY
        return {"Body": io.BytesIO(json.dumps(self.reference).encode())}


def run_task1(monkeypatch, tmp_path, selected, reference):
    s3 = FakeS3(select_payload(selected), reference)
    monkeypatch.setattr(run_extraction, "make_s3_client", lambda: s3)
    out = tmp_path / "out" / "hexes.geojson"
    return run_extraction.main(["--out", str(out)]), out, s3


def test_task1_success_writes_the_extract_and_exits_zero(monkeypatch, tmp_path):
    collection = make_collection()
    code, out, s3 = run_task1(monkeypatch, tmp_path, collection, collection)
    assert code == 0
    assert json.loads(out.read_text())["features"] == collection["features"]
    call = s3.select_calls[0]
    assert call["Key"] == "city-hex-polygons-8-10.geojson"
    assert '"resolution": 8 }' in call["Expression"]


def test_task1_low_conformance_exits_two_and_writes_nothing(monkeypatch, tmp_path):
    bad = make_collection(10)
    bad["type"] = "Nope"
    for feature in bad["features"]:
        feature["properties"]["resolution"] = 9
        feature["properties"]["index"] = "bad"
    code, out, _ = run_task1(monkeypatch, tmp_path, bad, bad)
    assert code == 2
    assert not out.exists()


def test_task1_difference_from_the_reference_exits_three_and_writes_nothing(monkeypatch, tmp_path):
    collection = make_collection()
    reference = make_collection()
    reference["features"].pop()
    code, out, _ = run_task1(monkeypatch, tmp_path, collection, reference)
    assert code == 3
    assert not out.exists()


def test_parse_collection_ignores_lines_it_does_not_recognise():
    feature = make_feature()
    parsed = parse_collection(["something else", feature_line(feature)])
    assert parsed["features"] == [feature]


# AWS client


def test_credentials_are_read_from_the_published_file(monkeypatch):
    body = json.dumps({"s3": {"access_key": "test-a", "secret_key": "test-s"}}).encode()
    seen = []

    def urlopen(url, timeout):
        seen.append(url)
        return io.BytesIO(body)

    monkeypatch.setattr(aws_client.urllib.request, "urlopen", urlopen)
    assert aws_client.fetch_credentials() == {"access_key": "test-a", "secret_key": "test-s"}
    assert seen == [aws_client.CREDS_URL]


def test_client_uses_the_challenge_region_and_those_credentials(monkeypatch):
    creds = {"access_key": "test-a", "secret_key": "test-s"}
    monkeypatch.setattr(aws_client, "fetch_credentials", lambda: creds)
    client = aws_client.make_s3_client()
    assert client.meta.region_name == "af-south-1"
    assert client._request_signer._credentials.access_key == "test-a"


# Task 2

CBD = (-33.9249, 18.4241)
CBD_CELL = h3.latlng_to_cell(*CBD, 8)
JOBURG = (-26.2041, 28.0473)


def write_gzip_csv(path, frame):
    frame.to_csv(path, index=False, compression="gzip")


def task2_inputs(data, rows, reference_cells):
    """Write the three inputs where download() will find them already cached."""
    data.mkdir(parents=True, exist_ok=True)
    sr = pd.DataFrame(rows, columns=["notification_number", "cause_code", "latitude", "longitude"])
    sr.insert(0, "Unnamed: 0", range(len(sr)))
    write_gzip_csv(data / sr_join.SR_FILE, sr)
    reference = sr.drop(columns=["Unnamed: 0"]).copy()
    reference[sr_join.INDEX_COLUMN] = reference_cells
    write_gzip_csv(data / sr_join.REFERENCE_FILE, reference)
    features = [{"properties": {"index": CBD_CELL}}]
    (data / sr_join.HEX_FILE).write_text(json.dumps({"features": features}))


def run_task2(tmp_path, *extra):
    out = tmp_path / "out" / "joined.csv.gz"
    return sr_join.main(["--data-dir", str(tmp_path / "data"), "--out", str(out), *extra]), out


def test_task2_success_writes_faithful_output_and_exits_zero(tmp_path):
    rows = [["000123", "None", *CBD], ["000124", "None", "", ""]]
    task2_inputs(tmp_path / "data", rows, [CBD_CELL, "0"])
    code, out = run_task2(tmp_path)
    assert code == 0
    written = pd.read_csv(out, dtype=str, keep_default_na=False)
    assert list(written[sr_join.INDEX_COLUMN]) == [CBD_CELL, "0"]
    assert list(written["notification_number"]) == ["000123", "000124"]
    assert list(written["cause_code"]) == ["None", "None"]
    assert "Unnamed: 0" not in written.columns


def test_task2_too_many_failed_joins_exits_four_and_writes_nothing(tmp_path):
    rows = [["1", "x", *CBD], ["2", "x", *JOBURG]]
    task2_inputs(tmp_path / "data", rows, [CBD_CELL, "0"])
    code, out = run_task2(tmp_path)
    assert code == 4
    assert not out.exists()


def test_task2_threshold_can_be_set_from_the_command_line(tmp_path):
    rows = [["1", "x", *CBD], ["2", "x", *JOBURG]]
    outside = h3.latlng_to_cell(*JOBURG, 8)
    task2_inputs(tmp_path / "data", rows, [CBD_CELL, outside])
    code, out = run_task2(tmp_path, "--max-join-error-rate", "0.9")
    assert code == 0
    assert out.exists()


def test_task2_unexpected_difference_from_the_reference_exits_three(tmp_path):
    other = h3.latlng_to_cell(CBD[0] + 0.1, CBD[1], 8)
    task2_inputs(tmp_path / "data", [["1", "x", *CBD]], [other])
    code, out = run_task2(tmp_path)
    assert code == 3
    assert not out.exists()


def test_duplicate_hexagons_in_the_grid_are_refused(tmp_path):
    path = tmp_path / "hex.geojson"
    feature = {"properties": {"index": CBD_CELL}}
    path.write_text(json.dumps({"features": [feature, feature]}))
    with pytest.raises(ValueError, match="duplicate"):
        sr_join.load_hex_index(path)


# The shared download() used by Tasks 2 and 5, against a real local server


class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        behaviour = self.server.behaviours.pop(0)
        if behaviour == "500":
            self.send_error(500)
            return
        self.send_response(200)
        extra = 100 if behaviour == "truncated" else 0
        self.send_header("Content-Length", str(len(self.server.body) + extra))
        self.end_headers()
        self.wfile.write(self.server.body)
        self.close_connection = True

    def log_message(self, *args):
        pass


@pytest.fixture
def server(monkeypatch):
    monkeypatch.setattr(sr_join.time, "sleep", lambda _: None)
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    srv.behaviours = []
    srv.body = gzip.compress(b"a,b\n1,2\n")
    srv.url = f"http://127.0.0.1:{srv.server_address[1]}/file.csv.gz"
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield srv
    srv.shutdown()
    srv.server_close()


def test_download_saves_the_file(server, tmp_path):
    server.behaviours = ["ok"]
    dest = tmp_path / "sub" / "file.csv.gz"
    assert sr_join.download(server.url, dest) == dest
    assert dest.read_bytes() == server.body


def test_download_reuses_an_existing_file_without_a_request(server, tmp_path):
    dest = tmp_path / "file.csv.gz"
    dest.write_bytes(b"cached")
    assert sr_join.download(server.url, dest) == dest
    assert dest.read_bytes() == b"cached"


def test_download_retries_a_server_error(server, tmp_path):
    server.behaviours = ["500", "ok"]
    dest = tmp_path / "file.csv.gz"
    sr_join.download(server.url, dest)
    assert dest.read_bytes() == server.body


def test_download_gives_up_after_the_retry_limit_and_leaves_no_file(server, tmp_path):
    server.behaviours = ["500", "500"]
    dest = tmp_path / "file.csv.gz"
    with pytest.raises(OSError):
        sr_join.download(server.url, dest, retries=2)
    assert not dest.exists()


def test_download_retries_a_truncated_file_and_never_keeps_it(server, tmp_path):
    server.behaviours = ["truncated", "ok"]
    dest = tmp_path / "file.csv.gz"
    sr_join.download(server.url, dest)
    assert dest.read_bytes() == server.body
    assert server.behaviours == []  # Both responses were used, so it really did retry.


def test_download_truncated_every_time_fails_and_leaves_no_file(server, tmp_path):
    server.behaviours = ["truncated", "truncated"]
    dest = tmp_path / "file.csv.gz"
    with pytest.raises(OSError, match="truncated"):
        sr_join.download(server.url, dest, retries=2)
    assert list(tmp_path.iterdir()) == []


# Task 5

CENTRE = (-33.5713, 18.4854)
CENTRE_CELL = h3.latlng_to_cell(*CENTRE, 8)


def service_requests():
    base = {
        "reference_number": "9",
        "creation_timestamp": "2020-02-01 00:30:00+02:00",
        "completion_timestamp": "2020-02-02 00:30:00+02:00",
        "directorate": "WATER",
        "department": "Distribution",
        "branch": "b",
        "section": "s",
        "code_group": "WATER",
        "code": "Burst",
        "cause_code_group": "",
        "cause_code": "",
        "official_suburb": "ROBINVALE",
        "latitude": str(CENTRE[0]),
        "longitude": str(CENTRE[1]),
        "h3_level8_index": CENTRE_CELL,
    }
    near = [{**base, "notification_number": f"{i:012d}"} for i in range(10)]
    rare = [{**base, "notification_number": "000000000099", "code": "Bees"}]
    far = [
        {**base, "notification_number": "000000000098", "latitude": "-33.9", "longitude": "18.4"}
    ]
    blank = [{**base, "notification_number": "000000000097", "latitude": "", "longitude": ""}]
    return pd.DataFrame(near + rare + far + blank)


@pytest.fixture
def task5(monkeypatch, tmp_path):
    """Real data folder and output folder, with only the two downloads replaced."""
    data = tmp_path / "data"
    data.mkdir()
    write_gzip_csv(data / sr_join.REFERENCE_FILE, service_requests())

    half = 0.005
    lat, lon = CENTRE
    ring = [
        [lon - half, lat - half],
        [lon + half, lat - half],
        [lon + half, lat + half],
        [lon - half, lat + half],
        [lon - half, lat - half],
    ]
    suburb = {
        "features": [
            {
                "type": "Feature",
                "properties": {SUBURB_NAME_FIELD: SUBURB_NAME},
                "geometry": {"type": "Polygon", "coordinates": [ring]},
            }
        ]
    }
    monkeypatch.setattr(
        atlantis_subsample,
        "fetch_bytes",
        lambda urls, path, validate: (json.dumps(suburb).encode(), "https://gis.example"),
    )
    spreadsheet = make_ods({"Sheet1": REAL_LAYOUT}, engine="openpyxl")

    def fetch_wind(urls, path, validate):
        validate(spreadsheet)
        return spreadsheet, "https://wind.example"

    monkeypatch.setattr(wind, "fetch_bytes", fetch_wind)

    out = tmp_path / "out"

    def run(*extra):
        return run_atlantis.main(["--data-dir", str(data), "--out-dir", str(out), *extra])

    run.out = out
    return run


def read(path):
    return pd.read_csv(path, dtype=str, keep_default_na=False)


def test_task5_success_writes_all_outputs_and_exits_zero(task5):
    assert task5() == 0
    names = {p.name for p in task5.out.iterdir()}
    assert names == {
        "atlantis_augmented.csv.gz",
        "atlantis_anonymised.csv.gz",
        "held_for_manual_review.csv",
        "summary.json",
    }


def test_task5_selects_only_requests_near_the_centroid(task5):
    task5()
    numbers = set(read(task5.out / "atlantis_augmented.csv.gz")["notification_number"])
    assert "000000000098" not in numbers  # Too far away.
    assert "000000000097" not in numbers  # No coordinates.
    assert "000000000000" in numbers


def test_task5_uses_atlantis_wind_and_anonymises_it(task5):
    task5()
    augmented = read(task5.out / "atlantis_augmented.csv.gz")
    assert set(augmented["wind_speed"]) == {"4.1"}
    anonymised = read(task5.out / "atlantis_anonymised.csv.gz")
    assert "wind_speed" not in anonymised.columns
    assert set(anonymised["wind_speed_6h_mean"]) == {"4.1"}
    assert set(anonymised["creation_timestamp"]) == {"2020-02-01 00:00"}


def test_task5_separates_records_for_review_and_records_the_counts(task5):
    task5()
    held = read(task5.out / "held_for_manual_review.csv")
    assert list(held["code"]) == ["Bees"]
    assert list(held["review_reason"]) == ["RARE_VALUE"]
    summary = json.loads((task5.out / "summary.json").read_text())
    assert summary["input_records"] == 11
    assert summary["anonymised_records"] == 10
    assert summary["held_for_review"] == 1


def test_task5_no_identifying_values_reach_the_anonymised_file(task5):
    task5()
    text = read(task5.out / "atlantis_anonymised.csv.gz").to_csv()
    for secret in ("000000000000", "ROBINVALE", str(CENTRE[0])):
        assert secret not in text


def test_task5_wind_unavailable_exits_five_and_writes_nothing(task5, monkeypatch):
    def down(urls, path, validate):
        raise FetchError("all down")

    monkeypatch.setattr(wind, "fetch_bytes", down)
    assert task5() == 5
    assert not task5.out.exists()


def test_task5_unreadable_wind_file_exits_five(task5, monkeypatch):
    useless = make_ods({"Sheet1": [["Name", "Value"], ["a", 1]]}, engine="openpyxl")
    monkeypatch.setattr(wind, "fetch_bytes", lambda urls, path, validate: (useless, "x"))
    assert task5() == 5


def test_task5_skip_wind_runs_without_wind_or_any_wind_download(task5, monkeypatch):
    def forbidden(urls, path, validate):
        raise AssertionError("wind must not be downloaded")

    monkeypatch.setattr(wind, "fetch_bytes", forbidden)
    assert task5("--skip-wind") == 0
    anonymised = read(task5.out / "atlantis_anonymised.csv.gz")
    assert not {"wind_speed", "wind_speed_6h_mean"} & set(anonymised.columns)


def test_task5_failed_self_check_exits_six_without_writing_the_anonymised_file(task5, monkeypatch):
    monkeypatch.setattr(
        run_atlantis, "check_precision", lambda original, out: {"ok": False, "problems": ["leak"]}
    )
    assert task5() == 6
    assert not (task5.out / "atlantis_anonymised.csv.gz").exists()
