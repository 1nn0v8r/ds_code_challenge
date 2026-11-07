"""Small timing helper so every pipeline step logs how long it took."""

import logging
import time
from contextlib import contextmanager


class Timer:
    elapsed = 0.0


@contextmanager
def timed(logger: logging.Logger, label: str):
    timer = Timer()
    start = time.perf_counter()
    try:
        yield timer
    finally:
        timer.elapsed = time.perf_counter() - start
        logger.info("%s took %.2fs", label, timer.elapsed)
