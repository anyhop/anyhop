#!/bin/bash
# Builds the MkDocs site into ./site. Two inputs are generated from their
# real source of truth and removed again on exit, so the working tree never
# carries a tracked duplicate:
#   - docs/index.md        <- README.md, via gen_docs_homepage.py (see there
#                              for what gets rewritten and why)
#   - docs/assets/icon.svg <- src/anyhop/assets/icon.svg (the app icon,
#                              referenced by mkdocs.yml as the site favicon)
#   - docs/assets/wordmark{,-dark}.svg <- src/anyhop/assets/wordmark{,-dark}.svg
#                              (the site logo; mkdocs.yml carries the light one
#                              with a #only-light fragment and extra CSS swaps
#                              in the dark one under the slate scheme)
set -euo pipefail
cd "$(dirname "$0")/.."

cleanup() {
	rm -f docs/index.md docs/assets/icon.svg \
		docs/assets/wordmark.svg docs/assets/wordmark-dark.svg
	rmdir docs/assets 2>/dev/null || true
}
trap cleanup EXIT

python3 scripts/gen_docs_homepage.py README.md docs/index.md
mkdir -p docs/assets
cp src/anyhop/assets/icon.svg docs/assets/icon.svg
cp src/anyhop/assets/wordmark.svg docs/assets/wordmark.svg
cp src/anyhop/assets/wordmark-dark.svg docs/assets/wordmark-dark.svg

uv run --group docs mkdocs build "$@"
