#!/usr/bin/env bash
# Render the HTML documents in docs/ to PDFs in pdf/.
#
# Chrome is pointed at a local HTTP server rather than file:// so that any
# webfonts resolve; on a machine with network access the PDFs pick up the
# real typefaces, and on one without they fall back to the declared stack.
set -euo pipefail

CHROME="${CHROME:-$(command -v google-chrome || command -v chromium || echo /Applications/Google\ Chrome.app/Contents/MacOS/Google\ Chrome)}"
OUT=pdf
TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT
mkdir -p "$OUT"

# Published artifacts get a doctype/head wrapper injected server-side; the
# files in docs/ are the raw body, so wrap them for a standalone render.
for src in docs/*.html; do
  name=$(basename "$src" .html)
  { printf '<!doctype html><html lang="en"><head><meta charset="utf-8">'
    printf '<meta name="viewport" content="width=device-width,initial-scale=1">'
    cat "$src"
    printf '</body></html>'
  } > "$TMP/$name.html"
done

python3 -m http.server 8731 --directory "$TMP" >/dev/null 2>&1 &
server=$!
trap 'kill $server 2>/dev/null; rm -rf "$TMP"' EXIT
sleep 2

for src in docs/*.html; do
  name=$(basename "$src" .html)
  "$CHROME" --headless --disable-gpu --no-sandbox --no-pdf-header-footer \
    --run-all-compositor-stages-before-draw --virtual-time-budget=20000 \
    --print-to-pdf="$OUT/$name.pdf" "http://127.0.0.1:8731/$name.html" 2>/dev/null
  echo "  $OUT/$name.pdf"
done
