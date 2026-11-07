"""Run every Data Engineering task in order with no human interaction.

Usage:  python src/run_all.py [--skip-wind]

Runs Task 1 (S3 Select extraction), Task 2 (service request join) and Task 5 (Atlantis
subsample, wind and anonymisation). A failing task does not stop the others, so one run shows
everything that is wrong. Inputs are downloaded to data/ and outputs written to output/, both
under the repository root wherever this is run from.

Exit code is 0 if every task succeeded, otherwise the exit code of the first task that failed
(see each task's documentation in docs/).
"""

import argparse
import logging
import os
import sys
from pathlib import Path

import run_atlantis
import run_extraction
import sr_join
from timing import timed

log = logging.getLogger("run_all")

REPO_ROOT = Path(__file__).resolve().parent.parent


def main(argv=None, root: Path = REPO_ROOT) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--skip-wind",
        action="store_true",
        help="run Task 5 without wind columns if the wind source is unavailable",
    )
    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)-7s %(name)s: %(message)s"
    )
    os.chdir(root)

    tasks = [
        ("Task 1: S3 Select extraction", run_extraction.main, []),
        ("Task 2: service request join", sr_join.main, []),
        ("Task 5: Atlantis pipeline", run_atlantis.main, ["--skip-wind"] if args.skip_wind else []),
    ]
    results = []
    with timed(log, "all tasks"):
        for name, task, task_args in tasks:
            log.info("=== %s", name)
            try:
                code = task(task_args)
            except Exception:  # One broken task must not hide the results of the others.
                log.exception("%s crashed", name)
                code = 1
            results.append((name, code))

    for name, code in results:
        log.log(
            logging.INFO if code == 0 else logging.ERROR,
            "%s: %s",
            name,
            "ok" if code == 0 else f"FAILED (exit {code})",
        )
    return next((code for _, code in results if code != 0), 0)


if __name__ == "__main__":
    sys.exit(main())
