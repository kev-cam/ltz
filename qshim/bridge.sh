#!/bin/bash
#
# bridge.sh — WSL side of the QSPICE-GUI-on-Xyce shim.
#
# Invoked by qspice-shim (standing in for QSPICE64.exe) with the engine's
# original arguments: `deck.cir [ -r deck.qraw ] [ -o deck.out ] [ -binary ]
# [ -ascii ] ...`. Windows paths arrive Windows-spelled; convert with wslpath.
# Pipeline: qspice2xyce.pl -> Xyce -> raw2qraw.pl -> the .qraw QUX displays.
# Any failure exits nonzero and the shim falls back to the real engine.
set -o pipefail

QSHIM_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
Q2X=/usr/local/src/xyce/utils/qspice2xyce.pl
XYCE=/usr/local/src/xyce-build/src/Xyce
export LD_LIBRARY_PATH="$HOME/xyce-libs"
LOG="$QSHIM_DIR/bridge.log"

log() { echo "[$(date '+%F %T')] $*" >> "$LOG"; }
die() { log "FAIL: $*"; exit 1; }

log "args: $*"

deck=""; qraw=""; out=""
while [ $# -gt 0 ]; do
  case "$1" in
    -r)      qraw="$2"; shift 2 ;;
    -o)      out="$2";  shift 2 ;;
    -binary|-ascii) shift ;;
    -simProcessID|-viewer) log "ignoring $1 $2"; shift 2 ;;   # QUX viewer handshake
    -*)      log "ignoring flag $1"; shift ;;
    *)       [ -z "$deck" ] && deck="$1"; shift ;;
  esac
done

# stdin-piped netlist (QUX marching-waveform mode): capture to a temp deck
if [ -z "$deck" ] && [ ! -t 0 ]; then
  deck=$(mktemp /tmp/qshim_XXXX.cir)
  cat > "$deck"
  log "netlist from stdin -> $deck"
fi
[ -n "$deck" ] || die "no input deck"

# Windows -> WSL paths
case "$deck" in [A-Za-z]:*|*\\*) deck=$(wslpath -u "$deck") || die "wslpath deck";; esac
[ -f "$deck" ] || die "deck not found: $deck"
base="${deck%.*}"
[ -n "$qraw" ] && case "$qraw" in [A-Za-z]:*|*\\*) qraw=$(wslpath -u "$qraw");; esac
[ -z "$qraw" ] && qraw="$base.qraw"

xdeck="$base.qshim.xyce.cir"
xraw="$base.qshim.raw"

perl "$Q2X" -o "$xdeck" "$deck" >> "$LOG" 2>&1 || die "qspice2xyce"
timeout 600 "$XYCE" -r "$xraw" -a "$xdeck" >> "$LOG" 2>&1 || die "Xyce"
[ -s "$xraw" ] || die "no Xyce rawfile"
perl "$QSHIM_DIR/raw2qraw.pl" "$xraw" "$deck" > "$qraw" || die "raw2qraw"
[ -s "$qraw" ] || die "empty qraw"
[ -n "$out" ] && { case "$out" in [A-Za-z]:*|*\\*) out=$(wslpath -u "$out");; esac
                   echo "Simulated by Xyce via qspice-shim" > "$out"; }
log "OK -> $qraw"
exit 0
