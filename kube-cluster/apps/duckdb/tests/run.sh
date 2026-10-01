#!/usr/bin/env bash
# The pod runs `INSTALL ui` at start, and duckdb publishes extensions some time after a
# release: a version without its `ui` extension crash-loops. Fail before it is deployed.
set -euo pipefail
dir="$1"
version=$(grep -oE 'duckdb/duckdb:[0-9][^@"[:space:]]*' "$dir/resources.yaml" | head -1 | cut -d: -f2)
[ -n "$version" ] || { echo "no duckdb/duckdb image tag in $dir/resources.yaml" >&2; exit 1; }
url="http://extensions.duckdb.org/v$version/linux_amd64/ui.duckdb_extension.gz"
code=$(curl -s -o /dev/null -w '%{http_code}' -I --retry 3 --retry-all-errors "$url")
if [ "$code" != 200 ]; then
  echo "duckdb $version: no ui extension published yet ($url -> HTTP $code)" >&2
  exit 1
fi
echo "duckdb $version: ui extension available"
