#!/usr/bin/env bash
# Suppress Pear Desktop's built-in tray icon so ytm-indicator is the only
# YouTube Music tray entry, without losing close-to-tray behavior.
#
# Upstream (th-ch/youtube-music) gates BOTH close-to-tray AND tray-icon
# creation behind the same `options.tray` config flag. We want one without
# the other, so we patch the packed asar:
#
#   dist/main/protocol-handler-*.js: setUpTray() early-returns unconditionally
#   dist/main/index.js:              unchanged — close-to-tray stays gated on
#                                    `options.tray`, which must remain true.
#
# The asar file is owned by pacman (package: pear-desktop). A pacman hook
# re-runs this script after every upgrade. See config/pear-notray.hook.
#
# Must run as root. Idempotent: running twice is a no-op.

set -euo pipefail

ASAR=/usr/lib/pear-desktop/app.asar
PRISTINE="${ASAR}.pristine"
WORK=$(mktemp -d)
trap 'rm -rf "$WORK"' EXIT

log() { printf '[patch-pear-notray] %s\n' "$*"; }
die() { printf '[patch-pear-notray] ERROR: %s\n' "$*" >&2; exit 1; }

[[ -f "$ASAR" ]] || die "$ASAR not found — is pear-desktop installed?"
[[ $EUID -eq 0 ]] || die "must run as root"
command -v npx >/dev/null || die "npx not on PATH (install nodejs/npm)"

# Extract current asar and look for the upstream setUpTray guard.
npx --yes @electron/asar extract "$ASAR" "$WORK" >/dev/null
TARGET=$(find "$WORK/dist/main" -name 'protocol-handler-*.js' -print -quit)
[[ -n "$TARGET" ]] || die "could not find protocol-handler-*.js in asar"

if grep -q 'if (!get("options.tray")) {' "$TARGET"; then
    log "patching $(basename "$TARGET")"
    sed -i 's|if (!get("options.tray")) {|if (true) {|' "$TARGET"
elif grep -q 'if (true) { .*tray = void 0' "$TARGET" \
  || (grep -n 'if (true) {' "$TARGET" | head -1 | grep -qE '^2[45]:'); then
    log "already patched — nothing to do"
    exit 0
else
    die "unexpected setUpTray() shape; refusing to patch (upstream changed?)"
fi

# Preserve the pristine asar once so we can always revert.
if [[ ! -f "$PRISTINE" ]]; then
    cp "$ASAR" "$PRISTINE"
    log "saved pristine copy to $PRISTINE"
fi

# Repack and swap atomically.
REPACKED="$WORK/patched.asar"
npx --yes @electron/asar pack "$WORK" "$REPACKED" >/dev/null
# @electron/asar pack writes the archive into the source tree it's packing
# in some versions; move explicitly if that happened.
[[ -s "$REPACKED" ]] || REPACKED="$WORK.asar"
install -m 0644 "$REPACKED" "$ASAR"
log "installed patched asar ($(stat -c '%s' "$ASAR") bytes)"
