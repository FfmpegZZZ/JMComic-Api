#!/bin/sh
# Stop at the first failed build; never attempt to pull private-looking local tags.
set -eu
cd "$(dirname "$0")/.."
sh scripts/build-comparison.sh --bench-only
docker compose -f compose.benchmark.yaml run --rm --no-deps --pull never prepare
docker compose -f compose.benchmark.yaml up -d --no-build --pull never baseline optimized
printf '\n%s\n' \
  'Comparison containers started:' \
  '  baseline:  http://127.0.0.1:18700' \
  '  optimized: http://127.0.0.1:18701'
