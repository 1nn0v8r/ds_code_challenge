import logging
import os
from pathlib import Path

import pytest

import run_all


@pytest.fixture
def tasks(monkeypatch):
    """Replace each task's main with a recorder. Set `codes` to control what each returns."""
    state = {"calls": [], "codes": {"extraction": 0, "join": 0, "atlantis": 0}}

    def fake(name, module):
        def main(argv):
            state["calls"].append((name, argv))
            result = state["codes"][name]
            if isinstance(result, Exception):
                raise result
            return result

        monkeypatch.setattr(module, "main", main)

    fake("extraction", run_all.run_extraction)
    fake("join", run_all.sr_join)
    fake("atlantis", run_all.run_atlantis)
    return state


@pytest.fixture(autouse=True)
def restore_working_directory(monkeypatch, tmp_path):
    # main() changes directory, and monkeypatch restores it afterwards.
    monkeypatch.chdir(tmp_path)


def test_all_tasks_run_in_order_and_success_exits_zero(tasks, tmp_path):
    assert run_all.main([], root=tmp_path) == 0
    assert tasks["calls"] == [("extraction", []), ("join", []), ("atlantis", [])]


def test_skip_wind_is_passed_to_the_atlantis_task_only(tasks, tmp_path):
    run_all.main(["--skip-wind"], root=tmp_path)
    assert tasks["calls"] == [("extraction", []), ("join", []), ("atlantis", ["--skip-wind"])]


def test_a_failed_task_does_not_stop_later_ones_and_sets_the_exit_code(tasks, tmp_path):
    tasks["codes"]["join"] = 4
    assert run_all.main([], root=tmp_path) == 4
    assert len(tasks["calls"]) == 3


def test_the_first_failure_decides_the_exit_code(tasks, tmp_path):
    tasks["codes"].update(extraction=3, atlantis=5)
    assert run_all.main([], root=tmp_path) == 3


def test_a_crashing_task_is_reported_and_later_tasks_still_run(tasks, tmp_path, caplog):
    tasks["codes"]["extraction"] = RuntimeError("boom")
    with caplog.at_level(logging.ERROR, logger="run_all"):
        assert run_all.main([], root=tmp_path) == 1
    assert len(tasks["calls"]) == 3
    assert "crashed" in caplog.text


def test_it_runs_from_the_repository_root_wherever_it_is_started(tasks, tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    run_all.main([], root=root)
    assert Path(os.getcwd()).resolve() == root.resolve()


def test_default_root_is_the_repository_root():
    assert (run_all.REPO_ROOT / "README.md").exists()
    assert (run_all.REPO_ROOT / "src" / "run_all.py").exists()
