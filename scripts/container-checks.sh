#!/bin/sh
set -eu
python -m pytest tests/ -W error --junitxml=/tmp/jmapi-tests.xml
ruff check .
ruff format --check .
mypy src
uv lock --check --offline
uv pip check
