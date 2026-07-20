#!/usr/bin/env bash
#
# Run the test suite with coverage measurement (audit T4).
#
# Produces a terminal report (with missing-line numbers), an XML report for CI,
# and a browsable HTML report under ./htmlcov/.  Coverage config lives in
# .coveragerc.  A plain `pytest` run is unaffected — coverage is opt-in here.
#
# Usage:
#   ./scripts/coverage.sh                 # full suite + reports
#   ./scripts/coverage.sh tests/test_tax.py   # scope to specific tests
#
set -euo pipefail

cd "$(dirname "$0")/.."

# Prefer the project virtualenv (system/anaconda pythons lack the deps).
PYTHON="${PYTHON:-.venv/bin/python}"
if [[ ! -x "$PYTHON" ]]; then
  PYTHON="python3"
fi

# Use the sys.monitoring coverage backend (Python 3.12+).  The default C tracer
# clashes with numpy's C extension under newer CPython ("cannot load module more
# than once per process"); sysmon avoids that and is faster besides.
export COVERAGE_CORE="${COVERAGE_CORE:-sysmon}"

echo "Running tests with coverage using $PYTHON ..."
"$PYTHON" -m pytest \
  --cov \
  --cov-config=.coveragerc \
  --cov-report=term-missing \
  --cov-report=xml:coverage.xml \
  --cov-report=html:htmlcov \
  "$@"

echo
echo "HTML report:  htmlcov/index.html"
echo "XML report:   coverage.xml"
