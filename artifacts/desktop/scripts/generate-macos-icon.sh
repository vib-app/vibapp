#!/bin/sh
set -eu

desktop_root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
source_svg=${1:-"$desktop_root/ui/favicon.svg"}
output_icns=${2:-"$desktop_root/src-tauri/icons/VibApp.icns"}
sips_bin=/usr/bin/sips
iconutil_bin=/usr/bin/iconutil

if [ ! -f "$source_svg" ] || [ -L "$source_svg" ]; then
  echo "macOS icon source is unavailable or unsafe: $source_svg" >&2
  exit 66
fi
if [ ! -x "$sips_bin" ] || [ ! -x "$iconutil_bin" ]; then
  echo "macOS icon tools are unavailable" >&2
  exit 69
fi

stage_root=$(mktemp -d "${TMPDIR:-/tmp}/vibapp-macos-icon.XXXXXX")
iconset="$stage_root/VibApp.iconset"
master="$stage_root/VibApp-1024.png"
staged_icns="$stage_root/VibApp.icns"

cleanup() {
  rm -rf -- "$stage_root"
}
trap cleanup EXIT
trap 'exit 129' HUP
trap 'exit 130' INT
trap 'exit 143' TERM

mkdir -p "$iconset"
"$sips_bin" -z 1024 1024 -s format png "$source_svg" --out "$master" >/dev/null

render_size() {
  filename=$1
  pixels=$2
  "$sips_bin" -z "$pixels" "$pixels" "$master" --out "$iconset/$filename" >/dev/null
}

render_size icon_16x16.png 16
render_size icon_16x16@2x.png 32
render_size icon_32x32.png 32
render_size icon_32x32@2x.png 64
render_size icon_128x128.png 128
render_size icon_128x128@2x.png 256
render_size icon_256x256.png 256
render_size icon_256x256@2x.png 512
render_size icon_512x512.png 512
render_size icon_512x512@2x.png 1024

"$iconutil_bin" -c icns "$iconset" -o "$staged_icns"
mkdir -p "$(dirname -- "$output_icns")"
cp "$staged_icns" "$output_icns"
chmod 644 "$output_icns"
/usr/bin/printf '%s\n' "$output_icns"
