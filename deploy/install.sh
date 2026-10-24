#!/bin/bash
# Installs (or removes) padspeaker on a Batocera. Run it on your LAPTOP, from anywhere:
#
#   deploy/install.sh [--dry-run] [--enable] [HOST]      install / update      (HOST default: root@batocera.local)
#   deploy/install.sh --uninstall [--dry-run] [HOST]     stop and remove the app and the service, KEEP config and logs
#   deploy/install.sh --uninstall --purge --yes [HOST]   remove everything of ours, including config.json and the logs
#
# Options: --dry-run | -n   print what would happen; nothing is sent to or changed on the Batocera
#          --host HOST      the same as the positional HOST (the PADSPEAKER_HOST environment variable also works)
#          --enable         also run `batocera-services enable padspeaker` and start the service. Without it the
#                           service is installed but NOT enabled, and the exact command to enable it is printed.
#          --min-free-mb N  require more than the default 50 MB free on /userdata
#
# What an install does (every remote step is a short POSIX sh script sent over ssh; read them below):
#   1. builds a tarball of padspeaker/ + tools/ + the service script + VERSION + MANIFEST.sha256 (no tests, docs, .venv, caches)
#   2. checks the Batocera BEFORE touching anything and lists every problem with its fix: at least 50 MB free on
#      /userdata, python3 (3.9+), libsbc.so.1 loadable, pactl and parec present (and batocera-services with --enable)
#   3. uploads the tarball and compares its sha256; extracts it into a NEW staging folder inside the install folder;
#      checks the manifest, that every .py compiles on the box's Python, that `import padspeaker` and
#      `python3 -m padspeaker --version` work - all BEFORE touching the installed version
#   4. swaps padspeaker/, tools/ and service/ in (renames; the old ones stay as padspeaker.previous/ etc.; a failure
#      rolls back) and puts the service script in /userdata/system/services/padspeaker (one atomic rename)
#   5. verifies the result (manifest, import, file count, service script identical and executable)
#   6. an install under the OLD name (ds4-bt-speaker, before the project was renamed to padspeaker) is migrated: its
#      service is stopped, disabled and removed, its config.json is copied to the new folder unless the new one already
#      has one (never overwritten), and its whole folder is MOVED to /userdata/system/ds4-bt-speaker.old-<timestamp>
#      (a backup that is never deleted). If the old service was enabled, the new one is enabled and started.
#      Old log files stay where they are. When there is no old install this step does nothing.
#   7. a service that was running is restarted so it runs the new code; with --enable the service is enabled and started
#
# NEVER touched: any other file on the box. Of our own files, config.json and the logs
# (/userdata/system/logs/padspeaker.log*) are kept by install and by --uninstall; only --uninstall --purge --yes
# deletes them.
#
# Written for macOS bash 3.2 (no associative arrays, no ${var,,}, no mapfile) and for POSIX sh on the Batocera.

set -eu
set -o pipefail

# ---------------------------------------------------------------- constants (the Batocera side)
DEST=/userdata/system/padspeaker
SERVICES_DIR=/userdata/system/services
SERVICE_NAME=padspeaker
LOG_FILE=/userdata/system/logs/padspeaker.log
# the names before the project was renamed (only the migration step and the uninstall note read these)
OLD_DEST=/userdata/system/ds4-bt-speaker
OLD_SERVICE_NAME=ds4_bt_speaker
OLD_LOG_FILE=/userdata/system/logs/ds4-bt-speaker.log
CONF_FILE=/userdata/system/batocera.conf
MIN_FREE_MB_FLOOR=50

# ---------------------------------------------------------------- options
HOST=${PADSPEAKER_HOST:-root@batocera.local}
DRY=0
UNINSTALL=0
PURGE=0
YES=0
ENABLE=0
MIN_FREE_MB=$MIN_FREE_MB_FLOOR
SSH_OPTS="-o BatchMode=yes -o ConnectTimeout=10"

say()  { printf '%s\n' "$*"; }
step() { printf '\n==> %s\n' "$*"; }
die()  { printf 'ERROR: %s\n' "$*" >&2; exit 1; }

usage() {
    sed -n '2,/^$/p' "$0" | sed 's/^# \{0,1\}//; /^$/d'
}

while [ $# -gt 0 ]; do
    case $1 in
        --dry-run|-n)    DRY=1 ;;
        --uninstall)     UNINSTALL=1 ;;
        --purge)         PURGE=1 ;;
        --yes|-y)        YES=1 ;;
        --enable)        ENABLE=1 ;;
        --host)          [ $# -ge 2 ] || die "--host needs a value"; HOST=$2; shift ;;
        --min-free-mb)   [ $# -ge 2 ] || die "--min-free-mb needs a value"; MIN_FREE_MB=$2; shift ;;
        -h|--help)       usage; exit 0 ;;
        -*)              die "unknown option: $1 (see --help)" ;;
        *)               HOST=$1 ;;
    esac
    shift
done

case $HOST in
    ''|-*|*[!A-Za-z0-9._@:-]*) die "that does not look like [user@]host: '$HOST'" ;;
esac
case $MIN_FREE_MB in
    ''|*[!0-9]*) die "--min-free-mb must be a whole number" ;;
esac
[ "$MIN_FREE_MB" -ge "$MIN_FREE_MB_FLOOR" ] || die "--min-free-mb cannot go below $MIN_FREE_MB_FLOOR"
[ "$PURGE" = 0 ] || [ "$UNINSTALL" = 1 ] || die "--purge only makes sense together with --uninstall"
[ "$ENABLE" = 0 ] || [ "$UNINSTALL" = 0 ] || die "--enable cannot be combined with --uninstall"
if [ "$PURGE" = 1 ] && [ "$YES" = 0 ] && [ "$DRY" = 0 ]; then
    die "--purge deletes config.json and the logs; add --yes if you mean it"
fi

ROOT=$(cd "$(dirname "$0")/.." && pwd)
TS=$(date +%Y%m%d-%H%M%S)-$$
STAGE=$DEST/.staging-$TS
STAGE_TGZ=$STAGE.tar.gz
PKG_DIR=""
CLEAN_REMOTE=0
PRELUDE_NOTE=""
REMOTE_OUT=""

# ---------------------------------------------------------------- local helpers
sha256_of() {
    if command -v sha256sum >/dev/null 2>&1; then
        sha256sum "$1" | awk '{print $1}'
    else
        shasum -a 256 "$1" | awk '{print $1}'
    fi
}

# ---------------------------------------------------------------- remote scripts
# Each function prints one POSIX sh script. The values it needs arrive from remote_prelude. Scripts are fed to `sh -s`
# on the Batocera, so nothing in them may read standard input (python and tar get </dev/null).

remote_prelude() {
    printf "DEST='%s'\nSERVICES_DIR='%s'\nSERVICE_NAME='%s'\nLOG_FILE='%s'\n" "$DEST" "$SERVICES_DIR" "$SERVICE_NAME" "$LOG_FILE"
    printf "STAGE='%s'\nSTAGE_TGZ='%s'\n" "$STAGE" "$STAGE_TGZ"
    printf "OLD_DEST='%s'\nOLD_SERVICE_NAME='%s'\nOLD_LOG_FILE='%s'\nCONF_FILE='%s'\nTS='%s'\n" "$OLD_DEST" "$OLD_SERVICE_NAME" "$OLD_LOG_FILE" "$CONF_FILE" "$TS"
    printf "MIN_FREE_MB='%s'\nEXPECT_SHA='%s'\nPURGE='%s'\nENABLE='%s'\n" "$MIN_FREE_MB" "${TARBALL_SHA:-}" "$PURGE" "$ENABLE"
    cat <<'EOF'
set -eu
die() { echo "ERROR: $*" >&2; exit 1; }
PY=$(command -v python3 || true)
EOF
}

script_preflight() {
    cat <<'EOF'
# Read-only. Every problem is listed with its fix, then the script stops if there was any.
problems=0
fail() { echo "FAIL: $*" >&2; problems=$((problems + 1)); }
[ -d /userdata/system ] || die "/userdata/system not found: is this a Batocera?"
for tool in tar gzip sha256sum awk df find cmp; do
    command -v "$tool" >/dev/null 2>&1 || fail "'$tool' is missing on this machine (it is part of every normal Linux userland; install coreutils/tar/gzip/findutils/diffutils)"
done
free=$(df -Pm /userdata | awk 'NR==2 {print $4}')
case $free in
    ''|*[!0-9]*) fail "cannot read the free space of /userdata" ;;
    *)
        echo "free space on /userdata: $free MB (need at least $MIN_FREE_MB MB)"
        [ "$free" -ge "$MIN_FREE_MB" ] || fail "only $free MB free on /userdata, need at least $MIN_FREE_MB MB. Free some space (delete unused ROMs, saves of games you do not play, downloads) and run the installer again"
        ;;
esac
if [ -z "$PY" ]; then
    fail "python3 not found. Install Python 3.9 or newer (Batocera ships python3; on Debian or Ubuntu: apt install python3)"
else
    echo "python: $("$PY" --version 2>&1) at $PY"
    "$PY" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)' </dev/null \
        || fail "python3 is older than 3.9; install Python 3.9 or newer"
    if "$PY" -c 'import ctypes; ctypes.CDLL("libsbc.so.1")' </dev/null 2>/dev/null; then
        echo "libsbc.so.1: loads"
    else
        fail "libsbc.so.1 (the SBC audio codec library) cannot be loaded. Install it: Debian or Ubuntu: apt install libsbc1; Fedora: dnf install sbc; Arch: pacman -S sbc. Batocera v41 ships it, so on Batocera this means the system is not a standard v41 build"
    fi
fi
for tool in pactl parec; do
    if command -v "$tool" >/dev/null 2>&1; then
        echo "$tool: found"
    else
        fail "'$tool' not found. It comes with the PulseAudio utilities that PipeWire's pulse layer uses: Debian or Ubuntu: apt install pulseaudio-utils; Fedora: dnf install pulseaudio-utils; Arch: pacman -S libpulse"
    fi
done
if [ "$ENABLE" = 1 ]; then
    command -v batocera-services >/dev/null 2>&1 || fail "batocera-services not found: --enable only works on a Batocera. Without --enable the files are installed and you can start the daemon by hand"
fi
if [ "$problems" -gt 0 ]; then
    die "$problems problem(s) found on this machine; nothing was changed"
fi
# not fatal: the audio server may simply not be up yet (the daemon waits for it)
if command -v pactl >/dev/null 2>&1; then
    T=""
    if command -v timeout >/dev/null 2>&1; then T="timeout 10"; fi
    if XDG_RUNTIME_DIR=/var/run $T pactl info </dev/null >/dev/null 2>&1; then
        echo "audio server: reachable with XDG_RUNTIME_DIR=/var/run"
    else
        echo "WARN: the audio server did not answer 'pactl info' with XDG_RUNTIME_DIR=/var/run. If PipeWire is running that is fine at install time; the daemon keeps retrying until it is up"
    fi
fi
if [ -d "$DEST" ]; then echo "an existing install was found in $DEST (it will be kept as .previous)"; fi
if [ -d "$OLD_DEST" ] || [ -f "$SERVICES_DIR/$OLD_SERVICE_NAME" ]; then
    echo "an install under the old name was found ($OLD_DEST, service $OLD_SERVICE_NAME): it will be migrated after the new version is in place (the old folder is kept as $OLD_DEST.old-<timestamp>)"
fi
if [ -f "$SERVICES_DIR/$SERVICE_NAME" ]; then
    echo "the service script exists; current state: $(sh "$SERVICES_DIR/$SERVICE_NAME" status </dev/null 2>&1 | tail -n 1 || true)"
fi
EOF
}

script_stage() {
    cat <<'EOF'
cd "$DEST"
got=$(sha256sum "$STAGE_TGZ" | awk '{print $1}')
[ "$got" = "$EXPECT_SHA" ] || die "the uploaded tarball is damaged (sha256 $got, expected $EXPECT_SHA); nothing was changed"
echo "tarball sha256 matches: $got"
bad=$(tar -tzf "$STAGE_TGZ" </dev/null | grep -E '(^/|(^|/)\.\.(/|$))' || true)
[ -z "$bad" ] || die "the tarball has unsafe paths: $bad"
rm -rf "$STAGE"
mkdir "$STAGE"
tar -xzf "$STAGE_TGZ" -C "$STAGE" </dev/null
for f in padspeaker/__init__.py padspeaker/__main__.py tools/box_selftest.py "service/$SERVICE_NAME" MANIFEST.sha256 VERSION; do
    [ -f "$STAGE/$f" ] || die "the tarball lacks $f"
done
(cd "$STAGE" && sha256sum -c MANIFEST.sha256 </dev/null >/dev/null) || die "MANIFEST.sha256 does not match the extracted files"
echo "manifest ok: $(wc -l < "$STAGE/MANIFEST.sha256" | tr -d ' ') files"
# From here on run from inside the staged copy: python puts the current folder first on sys.path, and in $DEST that
# would be the OLD padspeaker/ (the import test would then pass or fail for the wrong version).
cd "$STAGE"
# every file must compile on THIS Python (the laptop may be newer); nothing is written
STAGE_DIR="$STAGE" "$PY" -c '
import os, sys
root = os.environ["STAGE_DIR"]
bad = 0
for base, dirs, files in os.walk(root):
    for name in files:
        if name.endswith(".py"):
            path = os.path.join(base, name)
            try:
                compile(open(path, "rb").read(), path, "exec")
            except SyntaxError as exc:
                bad += 1
                print("syntax error:", path, exc)
sys.exit(1 if bad else 0)
' </dev/null || die "some files do not compile with this Python; nothing was changed"
STAGE_DIR="$STAGE" PYTHONPATH="$STAGE" PYTHONDONTWRITEBYTECODE=1 "$PY" -c '
import padspeaker, os
assert os.path.dirname(os.path.dirname(os.path.realpath(padspeaker.__file__))) == os.path.realpath(os.environ["STAGE_DIR"]), padspeaker.__file__
' </dev/null || die "'import padspeaker' fails on the staged copy; nothing was changed"
PYTHONPATH="$STAGE" PYTHONDONTWRITEBYTECODE=1 "$PY" -m padspeaker --version </dev/null \
    || die "'python3 -m padspeaker --version' fails on the staged copy; nothing was changed"
sh -n "$STAGE/service/$SERVICE_NAME" || die "the service script has a syntax error; nothing was changed"
echo "staged copy compiles, imports and answers --version"
EOF
}

script_swap() {
    cat <<'EOF'
cd "$DEST"
mkdir -p "$DEST" /userdata/system/logs "$SERVICES_DIR"
# 1. move the old app folders aside (kept as .previous), 2. move the new ones in; any failure puts the old ones back
for n in padspeaker tools service; do
    rm -rf "$n.previous"
    if [ -e "$n" ]; then mv "$n" "$n.previous"; fi
done
rollback() {
    echo "ERROR: could not move the new version in; restoring the old one" >&2
    for n in padspeaker tools service; do
        rm -rf "$n"
        if [ -e "$n.previous" ]; then mv "$n.previous" "$n"; fi
    done
    exit 1
}
for n in padspeaker tools service; do
    mv "$STAGE/$n" "$n" || rollback
done
for f in MANIFEST.sha256 VERSION; do
    cp "$STAGE/$f" "$DEST/.$f.new"
    mv -f "$DEST/.$f.new" "$DEST/$f"
done
# the Batocera service script: copied next to the final name inside $DEST (same filesystem), then ONE atomic rename
cp "$DEST/service/$SERVICE_NAME" "$DEST/.$SERVICE_NAME.new" || rollback
chmod 755 "$DEST/.$SERVICE_NAME.new"
mv -f "$DEST/.$SERVICE_NAME.new" "$SERVICES_DIR/$SERVICE_NAME" || rollback
echo "installed version: $(cat "$DEST/VERSION")"
EOF
}

script_verify() {
    cat <<'EOF'
cd "$DEST"
(sha256sum -c MANIFEST.sha256 </dev/null >/dev/null) || die "verification: installed files do not match MANIFEST.sha256"
have=$(find padspeaker tools service -type f ! -name '*.pyc' ! -path '*/__pycache__/*' | wc -l | tr -d ' ')
want=$(grep -cE ' (padspeaker|tools|service)/' MANIFEST.sha256 || true)
[ "$have" = "$want" ] || die "verification: $have files installed, the manifest lists $want"
PYTHONPATH="$DEST" PYTHONDONTWRITEBYTECODE=1 "$PY" -m padspeaker --version </dev/null >/dev/null \
    || die "verification: 'python3 -m padspeaker --version' fails in $DEST"
[ -x "$SERVICES_DIR/$SERVICE_NAME" ] || die "verification: $SERVICES_DIR/$SERVICE_NAME is not executable (does /userdata allow execute permission?)"
cmp -s "$SERVICES_DIR/$SERVICE_NAME" "$DEST/service/$SERVICE_NAME" || die "verification: the installed service script differs from the packaged one"
sh -n "$SERVICES_DIR/$SERVICE_NAME" || die "verification: the service script has a syntax error"
echo "files installed: $have (all match the manifest); --version: ok; service script executable and identical: ok"
echo "VERIFY-OK"
EOF
}

script_migrate() {
    cat <<'EOF'
# Moves an install under the old name (ds4-bt-speaker) to the new one. Runs after the new version is installed and
# verified, so a failed install never costs the old service. Nothing is deleted: the old folder is renamed.
old_svc="$SERVICES_DIR/$OLD_SERVICE_NAME"
if [ ! -d "$OLD_DEST" ] && [ ! -e "$old_svc" ] && [ ! -L "$old_svc" ]; then
    echo "no install under the old name ($OLD_SERVICE_NAME) found: nothing to migrate"
    exit 0
fi
[ "$OLD_DEST" != "$DEST" ] && [ "$OLD_SERVICE_NAME" != "$SERVICE_NAME" ] || die "refusing to migrate: the old and the new names are the same"
echo "found an install under the old name ($OLD_SERVICE_NAME): migrating it to $SERVICE_NAME"
was_enabled=0
if [ -f "$CONF_FILE" ] && sed -n 's|^system\.services=||p' "$CONF_FILE" | tr -d '"' | tr ' ' '\n' | grep -qx "$OLD_SERVICE_NAME"; then
    was_enabled=1
fi
# 1. stop the old service (it removes its sinks and restores the lightbar), 2. take it out of the enabled list,
# 3. remove its script
if [ -f "$old_svc" ]; then
    sh "$old_svc" stop </dev/null || die "could not stop the old service $OLD_SERVICE_NAME; the new version is installed and nothing of the old install was changed. Stop it by hand and run the installer again"
    if sh "$old_svc" status </dev/null >/dev/null 2>&1; then
        die "the old service $OLD_SERVICE_NAME is still running after 'stop'; nothing of the old install was changed. Stop it by hand and run the installer again"
    fi
    echo "stopped the old service $OLD_SERVICE_NAME"
fi
if [ "$was_enabled" = 1 ]; then
    if command -v batocera-services >/dev/null 2>&1; then
        batocera-services disable "$OLD_SERVICE_NAME" </dev/null || echo "WARN: batocera-services disable $OLD_SERVICE_NAME failed; remove it from system.services in batocera.conf by hand"
        echo "disabled the old service $OLD_SERVICE_NAME"
        echo "MIGRATE-ENABLE"
        echo "the old service was enabled, so $SERVICE_NAME will be enabled and started below"
    else
        echo "WARN: batocera-services not found; remove $OLD_SERVICE_NAME from system.services in batocera.conf by hand"
    fi
fi
if [ -e "$old_svc" ] || [ -L "$old_svc" ]; then
    rm -f "$old_svc"
    echo "removed the old service script $old_svc"
fi
# 4. the config is copied (never over an existing one), 5. the whole old folder is kept as a backup next to it
if [ -d "$OLD_DEST" ]; then
    if [ -f "$OLD_DEST/config.json" ]; then
        if [ -e "$DEST/config.json" ] || [ -L "$DEST/config.json" ]; then
            echo "kept the existing $DEST/config.json (the old config.json was not copied over it)"
        else
            cp -p "$OLD_DEST/config.json" "$DEST/.config.json.new" && mv "$DEST/.config.json.new" "$DEST/config.json" \
                || die "could not copy $OLD_DEST/config.json to $DEST/config.json; the old folder was left as it is"
            echo "copied $OLD_DEST/config.json to $DEST/config.json"
        fi
    fi
    backup="$OLD_DEST.old-$TS"
    [ ! -e "$backup" ] || die "$backup exists already; the old folder was left as it is"
    mv "$OLD_DEST" "$backup" || die "could not move $OLD_DEST to $backup; the old folder was left as it is"
    echo "kept the whole old folder as $backup (nothing was deleted; remove it yourself when you no longer need it)"
fi
for f in "$OLD_LOG_FILE"*; do
    if [ -e "$f" ]; then
        echo "left the old log files ($OLD_LOG_FILE*) where they are; the new log is $LOG_FILE"
        break
    fi
done
echo "MIGRATE-OK"
EOF
}

script_activate() {
    cat <<'EOF'
svc="$SERVICES_DIR/$SERVICE_NAME"
if [ "$ENABLE" = 1 ]; then
    batocera-services enable "$SERVICE_NAME" </dev/null || die "batocera-services enable $SERVICE_NAME failed (the files are installed)"
    batocera-services restart "$SERVICE_NAME" </dev/null || die "could not start the service (the files are installed; look at $LOG_FILE)"
    state=$(sh "$svc" status </dev/null 2>&1 | tail -n 1 || true)
    [ "$state" = started ] || die "the service is enabled but did not start (state: $state); look at $LOG_FILE"
    echo "service enabled and started ($state)"
elif sh "$svc" status </dev/null >/dev/null 2>&1; then
    sh "$svc" restart </dev/null || die "the service was running but could not be restarted (look at $LOG_FILE)"
    echo "the service was running: restarted to use the new version"
else
    echo "service not enabled and not running (as requested); nothing was started"
fi
EOF
}

script_cleanup() {
    cat <<'EOF'
case $STAGE in "$DEST"/.staging-*) rm -rf "$STAGE" "$STAGE_TGZ" ;; esac
EOF
}

script_uninstall() {
    cat <<'EOF'
svc="$SERVICES_DIR/$SERVICE_NAME"
if [ -d "$OLD_DEST" ] || [ -e "$SERVICES_DIR/$OLD_SERVICE_NAME" ]; then
    echo "NOTE: an install under the old name ($OLD_SERVICE_NAME) exists and is left alone; run the installer without --uninstall to migrate it, or remove it by hand"
fi
# stop the service, take it out of the enabled list, remove its script
if [ -f "$svc" ]; then
    sh "$svc" stop </dev/null || echo "WARN: could not stop the service cleanly"
fi
if command -v batocera-services >/dev/null 2>&1; then
    batocera-services disable "$SERVICE_NAME" </dev/null || echo "WARN: batocera-services disable failed"
else
    echo "WARN: batocera-services not found; if $SERVICE_NAME is listed in system.services in batocera.conf, remove it there"
fi
rm -f "$svc"
# the app itself
if [ -d "$DEST" ]; then
    cd "$DEST"
    rm -rf padspeaker padspeaker.previous tools tools.previous service service.previous
    rm -f VERSION MANIFEST.sha256 .*.new
    rm -rf .staging-*
fi
echo "removed the app and the service script"
if [ "$PURGE" = 1 ]; then
    [ "$DEST" = /userdata/system/padspeaker ] || die "refusing to purge an unexpected folder: $DEST"
    rm -rf "$DEST"
    rm -f "$LOG_FILE" "$LOG_FILE.1" "$LOG_FILE.2" "$LOG_FILE.3" "$LOG_FILE.console"
    echo "purged $DEST (config.json included) and the log files"
else
    if [ -d "$DEST" ]; then
        if rmdir "$DEST" 2>/dev/null; then
            echo "$DEST was empty and is removed too"
        else
            echo "kept your data in $DEST (use --purge --yes to delete it too):"
            ls -A "$DEST" | sed 's/^/    /'
        fi
    fi
    echo "kept the logs ($LOG_FILE*) too"
fi
EOF
}

# ---------------------------------------------------------------- running a remote step
# remote <description> <script function>
remote() {
    step "$1"
    if [ "$DRY" = 1 ]; then
        say "[dry-run] would run this on $HOST via 'ssh $SSH_OPTS $HOST sh -s'$PRELUDE_NOTE:"
        if [ -z "$PRELUDE_NOTE" ]; then
            remote_prelude | sed 's/^/    | /'
            PRELUDE_NOTE=" (after the same variable lines as above)"
        fi
        "$2" | sed 's/^/    | /'
        return 0
    fi
    # the output is also kept in $REMOTE_OUT, so the caller can look for a marker line
    # shellcheck disable=SC2086   # SSH_OPTS is a list of words on purpose
    { remote_prelude; "$2"; } | ssh $SSH_OPTS "$HOST" 'sh -s' | tee "${REMOTE_OUT:-/dev/null}" || die "step failed on $HOST: $1"
}

cleanup() {
    rc=$?
    if [ "$CLEAN_REMOTE" = 1 ]; then
        CLEAN_REMOTE=0
        # shellcheck disable=SC2086
        { remote_prelude; script_cleanup; } | ssh $SSH_OPTS "$HOST" 'sh -s' >/dev/null 2>&1 || true
    fi
    [ -z "$PKG_DIR" ] || rm -rf "$PKG_DIR"
    exit "$rc"
}
trap cleanup EXIT

# ---------------------------------------------------------------- building the tarball (local, harmless)
build_package() {
    [ -f "$ROOT/padspeaker/__init__.py" ] || die "padspeaker/__init__.py not found under $ROOT"
    [ -f "$ROOT/deploy/$SERVICE_NAME" ] || die "deploy/$SERVICE_NAME (the service script) is missing"
    PKG_DIR=$(mktemp -d "${TMPDIR:-/tmp}/padspeaker-pkg.XXXXXX")
    PKG=$PKG_DIR/pkg
    mkdir "$PKG"
    LIST=$PKG_DIR/files.txt
    # regular files only (no symlinks), without caches, editor leftovers or hidden files
    (cd "$ROOT" && find padspeaker tools -type f \
        ! -path '*/__pycache__/*' ! -name '*.pyc' ! -name '.*' ! -name '*.orig' ! -name '*.rej' \
        ! -name '*~' ! -name '*.swp' ! -name '*.log' | LC_ALL=C sort) > "$LIST"
    while IFS= read -r f; do
        mkdir -p "$PKG/$(dirname "$f")"
        cp -p "$ROOT/$f" "$PKG/$f"
    done < "$LIST"
    mkdir "$PKG/service"
    cp -p "$ROOT/deploy/$SERVICE_NAME" "$PKG/service/$SERVICE_NAME"
    chmod 755 "$PKG/service/$SERVICE_NAME"
    {
        ver=$(sed -n 's/^__version__ *= *["'"'"']\([^"'"'"']*\)["'"'"'].*/\1/p' "$ROOT/padspeaker/__init__.py" | head -n 1)
        [ -n "$ver" ] || ver=unknown
        if command -v git >/dev/null 2>&1 && git -C "$ROOT" rev-parse --short HEAD >/dev/null 2>&1; then
            rev=$(git -C "$ROOT" rev-parse --short HEAD)
            if [ -n "$(git -C "$ROOT" status --porcelain -- padspeaker tools deploy 2>/dev/null)" ]; then rev="$rev-dirty"; fi
        else
            rev=unknown
        fi
        echo "$ver (git $rev) built $(date -u +%Y-%m-%dT%H:%M:%SZ)"
    } > "$PKG/VERSION"
    # MANIFEST.sha256: "hash  path" for every packaged file, checkable with `sha256sum -c`
    (cd "$PKG" && find . -type f ! -name MANIFEST.sha256 | sed 's|^\./||' | LC_ALL=C sort | while IFS= read -r f; do
        printf '%s  %s\n' "$(sha256_of "$f")" "$f"
    done) > "$PKG/MANIFEST.sha256"

    if tar --version 2>&1 | grep -qi bsdtar; then
        OWNER_FLAGS="--no-xattrs --format ustar --uid 0 --gid 0 --uname root --gname root"   # plain ustar: no PAX/xattr headers for the Batocera tar
    else
        OWNER_FLAGS="--format=ustar --owner=0 --group=0 --numeric-owner"
    fi
    TARBALL=$PKG_DIR/padspeaker-$TS.tar.gz
    # shellcheck disable=SC2086
    (cd "$PKG" && COPYFILE_DISABLE=1 tar $OWNER_FLAGS -cf - padspeaker tools service MANIFEST.sha256 VERSION | gzip -n > "$TARBALL")
    TARBALL_SHA=$(sha256_of "$TARBALL")
    # last line of defence: nothing that does not belong may be in the tarball
    junk=$(tar -tzf "$TARBALL" | grep -E '__pycache__|\.pyc$|\.DS_Store|(^|/)\._|(^|/)(tests|\.venv|docs|\.git|node_modules)(/|$)|(^|/)config\.json$' || true)
    [ -z "$junk" ] || die "the tarball would contain files that do not belong in it: $junk"
    tar -tzf "$TARBALL" | grep -vE '/$' > "$PKG_DIR/tar-list.txt" || true
}

# ---------------------------------------------------------------- main
if [ "$UNINSTALL" = 1 ]; then
    [ "$DRY" = 0 ] || say "DRY RUN: nothing will be changed on $HOST."
    say "padspeaker: uninstall from $HOST"
    if [ "$PURGE" = 1 ]; then say "(--purge: config.json and the logs will be deleted too)"; else say "(config.json and the logs stay)"; fi
    remote "Stop the service, take it out of the enabled list, remove the app and the service script" script_uninstall
    if [ "$DRY" = 1 ]; then say ""; say "DRY RUN finished: nothing was changed."; fi
    exit 0
fi

[ "$DRY" = 0 ] || say "DRY RUN: nothing will be sent to or changed on $HOST."
say "padspeaker: install to $HOST:$DEST"

step "Build the tarball (on this computer)"
build_package
say "tarball: $TARBALL"
say "sha256:  $TARBALL_SHA"
say "version: $(cat "$PKG/VERSION")"
say "contents ($(wc -l < "$PKG_DIR/tar-list.txt" | tr -d ' ') files):"
sed 's/^/    /' "$PKG_DIR/tar-list.txt"

remote "Check the Batocera (needs python3, libsbc.so.1, pactl, parec and at least $MIN_FREE_MB MB free on /userdata)" script_preflight

step "Upload the tarball to $HOST:$STAGE_TGZ"
if [ "$DRY" = 1 ]; then
    say "[dry-run] would run: ssh $SSH_OPTS $HOST \"mkdir -p '$DEST' && cat > '$STAGE_TGZ'\" < tarball"
else
    CLEAN_REMOTE=1
    # SC2086: SSH_OPTS is a word list on purpose. SC2029: $DEST and $STAGE_TGZ are meant to expand here, on the
    # client; they are paths already validated above.
    # shellcheck disable=SC2086,SC2029
    ssh $SSH_OPTS "$HOST" "mkdir -p '$DEST' && cat > '$STAGE_TGZ'" < "$TARBALL" || die "the upload failed"
fi

remote "Unpack into a staging folder and check it (the installed version is still untouched)" script_stage
remote "Swap the new version in (old one kept as padspeaker.previous / tools.previous / service.previous)" script_swap
remote "Verify the installation" script_verify
if [ "$DRY" = 0 ]; then
    CLEAN_REMOTE=0
    # shellcheck disable=SC2086
    { remote_prelude; script_cleanup; } | ssh $SSH_OPTS "$HOST" 'sh -s' >/dev/null 2>&1 || true
fi
if [ "$DRY" = 0 ]; then REMOTE_OUT=$PKG_DIR/migrate.out; fi
remote "Migrate an install under the old name ($OLD_SERVICE_NAME), if there is one (the old folder is kept as a backup)" script_migrate
if [ "$DRY" = 0 ] && grep -qx 'MIGRATE-ENABLE' "$REMOTE_OUT"; then
    ENABLE=1      # the old service was enabled: carry that over, the activate step below enables and starts the new one
fi
REMOTE_OUT=""
if [ "$ENABLE" = 1 ]; then
    remote "Enable the service (batocera-services enable $SERVICE_NAME) and start it" script_activate
else
    remote "Restart the service only if it was already running" script_activate
fi

step "Done"
if [ "$DRY" = 1 ]; then
    say "DRY RUN finished: nothing was changed."
else
    say "installed to $DEST"
    if [ "$ENABLE" = 1 ]; then
        say "The service is enabled (it starts at every boot) and running. Log: $LOG_FILE"
        say "Next: Start > System Settings > Audio Output, choose the controller speaker."
    else
        say "The service is installed but NOT enabled. To enable it and start it now, run:"
        say "    ssh $HOST 'batocera-services enable $SERVICE_NAME && batocera-services start $SERVICE_NAME'"
        say "(or run this installer again with --enable; or Start > System Settings > Services on the Batocera)"
        say "To try it first, without enabling anything:"
        say "    ssh $HOST 'cd $DEST && python3 tools/box_selftest.py'"
    fi
fi
