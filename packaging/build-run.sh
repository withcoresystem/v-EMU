#!/usr/bin/env bash
# Build a self-extracting release installer from the current Git tree.
#
# The resulting .run contains only runtime/install files. Development notes
# and the test suite are deliberately excluded from public artifacts.
set -euo pipefail

ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
VERSION=${1:-$(cd "$ROOT" && python3 -c 'import tomllib; print(tomllib.load(open("pyproject.toml","rb"))["project"]["version"])' 2>/dev/null || echo 0.3.0)}
OUT_DIR=${2:-"$ROOT/dist"}
OUT="$OUT_DIR/vemu-${VERSION}-x86_64.run"

if [[ -n $(git -C "$ROOT" status --porcelain) ]]; then
    echo "Working tree is not clean; commit or stash changes before building." >&2
    exit 1
fi

TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT
mkdir -p "$TMP/vEMU"
git -C "$ROOT" archive --format=tar HEAD | tar -x -C "$TMP/vEMU"

# Public artifact: no development docs or test suite.
rm -rf \
    "$TMP/vEMU/.gitignore" \
    "$TMP/vEMU/tests" \
    "$TMP/vEMU/docs" \
    "$TMP/vEMU/contrib"
# Keep the runtime/install surface that the installer needs.
mkdir -p "$TMP/vEMU/docs" "$TMP/vEMU/docs/images" "$TMP/vEMU/contrib/windows"
cp "$ROOT/MANUAL.md" "$ROOT/BUILDER.md" "$ROOT/README.md" \
    "$ROOT/GRAPHICS.md" "$ROOT/LICENSE" "$ROOT/NOTICE" \
    "$ROOT/DISCLAIMER.md" "$ROOT/CONTRIBUTING.md" "$ROOT/SECURITY.md" \
    "$TMP/vEMU/"
cp "$ROOT/docs/USER-GUIDE.md" "$ROOT/docs/ARCHITECTURE.md" \
    "$ROOT/docs/ARCH-EXPERIMENTAL.md" "$ROOT/docs/BUILDER-BRANDING.md" \
    "$ROOT/docs/WINDOWS-SEAMLESS.md" "$ROOT/docs/WINDOWS-3D-PREVIEW.md" \
    "$ROOT/docs/RELEASING.md" "$TMP/vEMU/docs/"
cp "$ROOT/docs/images/"* "$TMP/vEMU/docs/images/"
mkdir -p "$TMP/vEMU/docs/release-notes"
cp "$ROOT/docs/release-notes/"*.md \
    "$TMP/vEMU/docs/release-notes/"
cp "$ROOT/contrib/windows/README.md" \
    "$ROOT/contrib/windows/profiles.json" \
    "$ROOT/contrib/windows/vemu-guest-optimize.ps1" \
    "$TMP/vEMU/contrib/windows/"

chmod +x "$TMP/vEMU/setup.sh" "$TMP/vEMU/packaging/vemu-installer.sh"
tar -C "$TMP" -czf "$TMP/payload.tar.gz" vEMU

mkdir -p "$OUT_DIR"
{
    cat <<'HEADER'
#!/usr/bin/env bash
set -euo pipefail
PAYLOAD_LINE=$(awk '/^__VEMU_PAYLOAD__$/{print NR+1; exit}' "$0")
QUICK=0
for arg in "$@"; do
    case "$arg" in
        --dry-run|--help|-h) QUICK=1 ;;
    esac
done
if [[ $QUICK -eq 1 ]]; then
    TMPDIR_RUN=$(mktemp -d)
    trap 'rm -rf "$TMPDIR_RUN"' EXIT
    tail -n +"$PAYLOAD_LINE" "$0" | tar -xzf - -C "$TMPDIR_RUN"
    "$TMPDIR_RUN/vEMU/packaging/vemu-installer.sh" "$@"
    exit $?
fi
DEST=${VEMU_SOURCE_DIR:-${XDG_DATA_HOME:-$HOME/.local/share}/vemu/source}
mkdir -p "$DEST"
rm -rf "$DEST/vEMU"
tail -n +"$PAYLOAD_LINE" "$0" | tar -xzf - -C "$DEST"
: > "$DEST/vEMU/.vemu-run-source"
exec "$DEST/vEMU/packaging/vemu-installer.sh" "$@"
__VEMU_PAYLOAD__
HEADER
    cat "$TMP/payload.tar.gz"
} > "$OUT"
chmod +x "$OUT"
(
    cd "$OUT_DIR"
    sha256sum "$(basename "$OUT")" > "$(basename "$OUT").sha256"
)
echo "Built: $OUT"
echo "Checksum: $OUT.sha256"
