#!/usr/bin/env bash
# r2-cp.sh — copy one file to or from R2, trying again when the copy fails.
# Used by build-charts.yml and build-mesh.yml at every `aws s3 cp` of a
# single file.
#
#   r2-cp.sh <source> <destination>      (either may be s3://bucket/key)
#
# A copy to R2 can fail although nothing is wrong with the file: on
# 2026-11-01 two of 27 chart files failed once each with "InvalidPart …
# CompleteMultipartUpload: One or more of the specified parts could not be
# found", were not tried again, and the run was marked failed. The copy is
# tried again as the workflows' downloads are (curl --retry 3 --retry-delay
# 10): up to RETRIES more times, DELAY_S apart. Exit status: 0 when a try
# succeeded, else the last try's status.
#
# Environment: ENDPOINT (R2 S3 endpoint), AWS credentials for `aws s3 cp`.
set -uo pipefail

RETRIES=3
DELAY_S=10

if [ "$#" -ne 2 ]; then
  echo "usage: r2-cp.sh <source> <destination>" >&2
  exit 2
fi

try=0
while :; do
  aws s3 cp --endpoint-url "$ENDPOINT" --no-progress "$1" "$2"
  rc=$?
  if [ "$rc" -eq 0 ]; then
    exit 0
  fi
  if [ "$try" -ge "$RETRIES" ]; then
    echo "r2-cp: $1 -> $2 failed after $((try + 1)) tries (status $rc)" >&2
    exit "$rc"
  fi
  try=$((try + 1))
  echo "r2-cp: $1 -> $2 failed (status $rc); try $((try + 1)) of $((RETRIES + 1)) in ${DELAY_S} s" >&2
  sleep "$DELAY_S"
done
