#!/bin/bash
set -euo pipefail

root="$(cd "$(dirname "$0")/.." && pwd)"
version="${1:-0.2.0}"
python="${DRAWCHECK_BUILD_PYTHON:-$root/.venv/bin/python}"
output="$root/packaging/output/macos"
image="$output/DrawCheck-$version-macos-arm64.dmg"

if [[ "$(uname -sm)" != "Darwin arm64" ]]; then
    echo "Build on Apple Silicon with a native arm64 Python." >&2
    exit 2
fi
if [[ ! "$version" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
    echo "Use a version in major.minor.patch format." >&2
    exit 2
fi
if [[ -e "$image" ]]; then
    echo "A disk image already exists at $image. Move it before rebuilding." >&2
    exit 2
fi

export PATH="/opt/homebrew/bin:/usr/local/bin:$PATH"
export DRAWCHECK_BUILD_VERSION="$version"
work="$(mktemp -d "${TMPDIR:-/tmp}/drawcheck-macos.XXXXXXXX")"
trap 'rm -rf -- "$work"' EXIT
export PYINSTALLER_CONFIG_DIR="$work/cache"

cd "$root"
"$python" scripts/check-environment.py
mkdir -p "$output"
"$python" -m PyInstaller --noconfirm --clean --distpath "$work/dist" \
    --workpath "$work/build" packaging/drawcheck-macos.spec
app="$work/dist/DrawCheck.app"
report="$output/frozen-self-test-$version-macos-arm64.json"
PATH=/usr/bin:/bin "$app/Contents/MacOS/DrawCheck" --self-test "$report"
codesign --verify --deep --strict "$app"
mkdir "$work/image"
mv "$app" "$work/image/"
ln -s /Applications "$work/image/Applications"
hdiutil create -volname DrawCheck -srcfolder "$work/image" -format UDZO "$work/DrawCheck.dmg"
hdiutil verify "$work/DrawCheck.dmg"
mv "$work/DrawCheck.dmg" "$image"
(cd "$output" && shasum -a 256 "$(basename "$image")" > "$(basename "$image").sha256")
echo "Built $image"
