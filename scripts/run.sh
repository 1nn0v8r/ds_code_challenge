#!/usr/bin/env sh
# Set up .venv (if needed), install requirements, then run the pipeline.
#
# Usage (macOS, Linux, or Git Bash on Windows):
#   sh scripts/run.sh               # run the full pipeline
#   sh scripts/run.sh --skip-wind   # run without the wind data
#   sh scripts/run.sh --test        # run the test suite instead
#
# Picks the first installed Python that is 3.12 or newer, preferring 3.13.

set -eu

REPO_ROOT=$(cd "$(dirname "$0")/.." && pwd)
cd "$REPO_ROOT"

VENV=.venv

is_supported() {
    "$1" -c "import sys; sys.exit(0 if sys.version_info >= (3, 12) else 1)" >/dev/null 2>&1
}

# A version match is not enough: some system Pythons (for example Debian's
# python3.13 without python3-venv) cannot create a venv. Check by creating one.
can_create_venv() {
    probe_dir=$(mktemp -d) || return 1
    "$1" -m venv "$probe_dir/venv" >/dev/null 2>&1
    status=$?
    rm -rf "$probe_dir"
    return $status
}

find_python() {
    # PYTHON_BIN=/path/to/python overrides the search (it must still pass the checks).
    if [ -n "${PYTHON_BIN:-}" ] && is_supported "$PYTHON_BIN" && can_create_venv "$PYTHON_BIN"; then
        echo "$PYTHON_BIN"
        return 0
    fi
    for cand in python3.13 python3.12 python3.14 python3.15 python3 python; do
        if command -v "$cand" >/dev/null 2>&1 && is_supported "$cand" && can_create_venv "$cand"; then
            echo "$cand"
            return 0
        fi
    done
    return 1
}

venv_python() {
    if [ -x "$VENV/bin/python" ]; then
        echo "$VENV/bin/python"
    elif [ -x "$VENV/Scripts/python.exe" ]; then
        echo "$VENV/Scripts/python.exe"
    fi
}

VENV_PY=$(venv_python || true)

if [ -n "$VENV_PY" ] && ! is_supported "$VENV_PY"; then
    echo "Existing $VENV was built with Python older than 3.12. Recreating it."
    rm -rf "$VENV"
    VENV_PY=""
fi

if [ -z "$VENV_PY" ]; then
    BASE_PY=$(find_python) || {
        echo "Python 3.12 or newer is required, but none was found on PATH." >&2
        echo "Install Python 3.13 from https://www.python.org/downloads/ and retry." >&2
        exit 1
    }
    echo "Creating $VENV with $BASE_PY"
    "$BASE_PY" -m venv "$VENV"
    VENV_PY=$(venv_python)
fi

echo "Installing requirements"
"$VENV_PY" -m pip install -q --disable-pip-version-check -r requirements.txt

if [ "${1:-}" = "--test" ]; then
    shift
    exec "$VENV_PY" -m pytest "$@"
fi

exec "$VENV_PY" src/run_all.py "$@"
