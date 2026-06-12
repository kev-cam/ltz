#!/bin/bash
#
# ltwatch.sh <dir> — LTspice-GUI-on-Xyce watcher (runs in WSL).
#
# LTspice.exe is monolithic (no engine exe to shim, unlike QSPICE), so the
# integration is: LTspice = schematic editor + waveform viewer, Xyce = engine,
# this watcher = the glue. Save a .asc in the watched dir and it:
#   1. netlists it with LTspice's own netlister (-netlist, via interop)
#   2. translates + simulates with ltz/Xyce
#   3. writes <name>.raw in LTspice ASCII raw format next to the schematic
# Open the .raw in LTspice (File -> Open) to view the Xyce waveform.
#
# Polling (1s mtime scan): inotify doesn't work on /mnt/c drvfs.
set -u

WATCH="${1:?usage: ltwatch.sh <dir-with-.asc>}"
LT="/mnt/c/Program Files/ADI/LTspice/LTspice.exe"
LTZ=/usr/local/src/ltz/bin/ltz
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
export XYCE=/usr/local/src/xyce-build/src/Xyce
export LD_LIBRARY_PATH="$HOME/xyce-libs"
LOG="$HERE/ltwatch.log"

log() { echo "[$(date '+%F %T')] $*" | tee -a "$LOG"; }

declare -A seen
log "watching $WATCH (save a .asc to simulate on Xyce)"
while :; do
  for asc in "$WATCH"/*.asc; do
    [ -e "$asc" ] || continue
    mt=$(stat -c %Y "$asc" 2>/dev/null) || continue
    key="$asc"
    if [ "${seen[$key]:-}" != "$mt" ]; then
      # skip the initial scan: only simulate saves made after startup
      if [ -n "${seen[$key]:-}" ] || [ "${LTWATCH_INITIAL:-0}" = "1" ]; then
        base="${asc%.asc}"
        name="$(basename "$base")"
        log "change: $name.asc"
        rm -f "$base.net"
        timeout 120 "$LT" -netlist "$(wslpath -w "$asc")" >/dev/null 2>&1
        if [ ! -s "$base.net" ]; then
          log "  $name: LTspice -netlist produced nothing (missing symbols? dialog?)"
        else
          xr="$base.ltwatch.xraw"
          rm -f "$xr"
          if timeout 600 "$LTZ" -b "$base.net" -r "$xr" >> "$LOG" 2>&1 && [ -s "$xr" ]; then
            perl "$HERE/raw2ltraw.pl" "$xr" "$(wslpath -w "$asc")" > "$base.raw" \
              && log "  $name: Xyce OK -> $name.raw (open it in LTspice)" \
              || log "  $name: raw conversion failed"
          else
            log "  $name: translate/Xyce failed (see $LOG)"
          fi
        fi
      fi
      seen[$key]="$mt"
    fi
  done
  sleep 1
done
