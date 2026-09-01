#!/bin/sh
set -eu

script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
web_dir=$(CDPATH= cd -- "$script_dir/.." && pwd)
workspace_dir=$(CDPATH= cd -- "$web_dir/../.." && pwd)
temporary_dir=$(mktemp -d)
trap 'rm -rf "$temporary_dir"' EXIT HUP INT TERM

cd "$web_dir"
./node_modules/.bin/openapi-ts \
  -i "$workspace_dir/docs/contracts/openapi.json" \
  -o "$temporary_dir/generated"
diff -ru "$web_dir/src/api/generated" "$temporary_dir/generated"
