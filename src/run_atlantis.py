"""Task 5 entry point: Atlantis subsample, wind augmentation, anonymisation.

Usage:  python src/run_atlantis.py [--skip-wind]

Inputs are downloaded to data/ if absent (sr_hex.csv.gz is the Task 2 reference file).
Outputs (all under output/atlantis/):
  atlantis_augmented.csv.gz   subsample + wind, NOT anonymised (contains personal data)
  atlantis_anonymised.csv.gz  safe to share
  held_for_manual_review.csv  NOT anonymised; restricted, for a person to anonymise by hand

Exit codes: 0 ok, 5 wind data unavailable (use --skip-wind to proceed without it),
6 anonymisation self-check failed.
"""
import argparse
import json
import logging
import sys
from pathlib import Path

import pandas as pd

from anonymise import anonymise, check_precision
from atlantis_subsample import get_atlantis_centroid, subsample
from http_fetch import FetchError
from sr_join import BASE_URL, REFERENCE_FILE, download
from timing import timed
from wind import attach_wind, load_wind

log = logging.getLogger("run_atlantis")

DEFAULT_DATA_DIR = Path("data")
DEFAULT_OUT_DIR = Path("output") / "atlantis"


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, default=DEFAULT_DATA_DIR)
    parser.add_argument("--out-dir", type=Path, default=DEFAULT_OUT_DIR)
    parser.add_argument("--skip-wind", action="store_true",
                        help="continue without wind columns if the wind source is unavailable")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(name)s: %(message)s")

    with timed(log, "total pipeline"):
        sr_path = download(BASE_URL + REFERENCE_FILE, args.data_dir / REFERENCE_FILE)
        with timed(log, "read sr_hex.csv.gz"):
            sr = pd.read_csv(sr_path, dtype=str, keep_default_na=False)

        centre = get_atlantis_centroid(args.data_dir)
        sample = subsample(sr, centre)

        if args.skip_wind:
            log.warning("--skip-wind set: output will have NO wind columns")
            augmented = sample
        else:
            try:
                wind = load_wind(args.data_dir)
            except (FetchError, ValueError) as exc:
                log.error("wind data unavailable: %s", exc)
                log.error("re-run later, or pass --skip-wind to proceed without wind columns")
                return 5
            augmented = attach_wind(sample, wind)

        args.out_dir.mkdir(parents=True, exist_ok=True)
        augmented.to_csv(args.out_dir / "atlantis_augmented.csv.gz", index=False)

        result = anonymise(augmented)
        check = check_precision(augmented, result.anonymised)
        if not check["ok"]:
            log.error("anonymisation self-check failed: %s", check["problems"])
            return 6

        result.anonymised.to_csv(args.out_dir / "atlantis_anonymised.csv.gz", index=False)
        result.held_for_review.to_csv(args.out_dir / "held_for_manual_review.csv", index=False)
        (args.out_dir / "summary.json").write_text(json.dumps(result.summary, indent=2))
        log.warning(
            "%s contains personal data: restrict access and do not share it",
            args.out_dir / "held_for_manual_review.csv",
        )
    log.info("done")
    return 0


if __name__ == "__main__":
    sys.exit(main())
