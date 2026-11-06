#!/bin/sh
set -eu
cd "$(dirname "$0")/.."
BENCH_ONLY=false
case "${1:-}" in
  "") ;;
  --bench-only) BENCH_ONLY=true ;;
  *) printf '%s\n' 'Usage: sh scripts/build-comparison.sh [--bench-only]' >&2; exit 2 ;;
esac
BENCH_STAGE='checking Docker access'
report_build_failure() {
  BENCH_EXIT_CODE=$?
  if [ "$BENCH_EXIT_CODE" -ne 0 ]; then
    printf '\n%s\n' "Comparison build failed while $BENCH_STAGE (exit $BENCH_EXIT_CODE)." >&2
    printf '%s\n' 'Fix the first error above, then rerun this script. The comparison images are local builds; docker login is not required for them.' >&2
  fi
}
trap report_build_failure EXIT
docker info --format '{{.ServerVersion}}'
# A fixed baseline makes the experiment reproducible even after local commits.
BASELINE_REF=${BASELINE_REF:-feb1cc6}
BENCH_STAGE="preparing baseline $BASELINE_REF"
git rev-parse --verify "${BASELINE_REF}^{commit}" >/dev/null
mkdir -p .benchmark/baseline
# Never reset/stash the user's working tree.
git archive --output=.benchmark/baseline.tar "$BASELINE_REF"
tar -xf .benchmark/baseline.tar -C .benchmark/baseline
# Copying only the harness Dockerfile does not modify baseline production code.
cp scripts/benchmark/Dockerfile .benchmark/baseline/Dockerfile.benchmark
BENCH_STAGE='building the baseline image'
printf '\n%s\n' '[1/2] Building jmcomic-api:bench-baseline'
docker build -f .benchmark/baseline/Dockerfile.benchmark -t jmcomic-api:bench-baseline .benchmark/baseline
docker image inspect jmcomic-api:bench-baseline --format '{{.Id}}'
BENCH_STAGE='building the optimized image'
printf '\n%s\n' '[2/2] Building jmcomic-api:bench-optimized'
docker build -f scripts/benchmark/Dockerfile -t jmcomic-api:bench-optimized .
docker image inspect jmcomic-api:bench-optimized --format '{{.Id}}'
if [ "$BENCH_ONLY" = true ]; then
  printf '\n%s\n' 'Both local comparison images are ready.'
  exit 0
fi
BENCH_STAGE='building the Python 3.12 test image'
docker build --target test --build-arg PYTHON_VERSION=3.12 -t jmcomic-api:test-py312 .
BENCH_STAGE='building the Python 3.13 test image'
docker build --target test --build-arg PYTHON_VERSION=3.13 -t jmcomic-api:test-py313 .
BENCH_STAGE='building the production image'
docker build -t jmcomic-api:optimized .
printf '%s\n' 'Images ready. See docs/docker-comparison.md for test and startup commands.'
