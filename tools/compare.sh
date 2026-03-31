#!/bin/bash
#
# compare.sh — Run LTspice examples through both LTspice and Xyce, compare results
#
# Usage:
#   ./tools/compare.sh                           # run Educational examples
#   ./tools/compare.sh examples/Educational      # specific directory
#   ./tools/compare.sh examples/Educational/opamp.asc  # single file
#

set -euo pipefail

LTSPICE_DIR="/home/dkc/.wine/drive_c/Program Files/LTC/LTspiceXVII"
LTSPICE_EXE="$LTSPICE_DIR/XVIIx64.exe"
LTSPICE_LIB="$LTSPICE_DIR/lib/sub"
XYCE="${XYCE:-/usr/local/src/Xyce-8/xyce-build/src/Xyce}"
LTZ="$(dirname "$0")/../bin/ltz"
WORK="/tmp/ltz_compare"
TIMEOUT_LTSPICE=60
TIMEOUT_XYCE=60

# Counters
total=0
ltspice_ok=0
ltspice_fail=0
xyce_ok=0
xyce_fail=0
convert_fail=0
skip=0

# Results log
mkdir -p "$WORK"
RESULTS="$WORK/results.csv"
echo "file,ltspice_status,ltspice_time,xyce_status,xyce_time,notes" > "$RESULTS"

log() { echo "  $*"; }
warn() { echo "  WARN: $*" >&2; }

# ── LTspice netlist export ─────────────────────────────────────────────
ltspice_netlist() {
    local asc="$1" net="$2"
    # Convert unix path to windows path for wine
    local win_path
    win_path=$(winepath -w "$asc" 2>/dev/null) || win_path="$asc"

    timeout "$TIMEOUT_LTSPICE" xvfb-run -a wine64 "$LTSPICE_EXE" \
        -netlist "$win_path" 2>/dev/null

    # LTspice writes .net next to the .asc
    local base="${asc%.asc}"
    if [[ -f "${base}.net" ]]; then
        cp "${base}.net" "$net"
        return 0
    fi
    return 1
}

# ── LTspice simulation ────────────────────────────────────────────────
ltspice_run() {
    local asc="$1" raw="$2"
    local win_path
    win_path=$(winepath -w "$asc" 2>/dev/null) || win_path="$asc"

    # Remove old .raw
    local base="${asc%.asc}"
    rm -f "${base}.raw" "${base}.log"

    # -Run simulates and keeps GUI open; we poll for .raw then kill
    xvfb-run -a wine64 "$LTSPICE_EXE" -Run "$win_path" 2>/dev/null &
    local pid=$!

    local elapsed=0
    while (( elapsed < TIMEOUT_LTSPICE )); do
        sleep 1
        (( elapsed++ ))
        if [[ -f "${base}.raw" ]]; then
            # Wait a moment for write to complete
            local size1 size2
            size1=$(stat -c%s "${base}.raw" 2>/dev/null || echo 0)
            sleep 1
            size2=$(stat -c%s "${base}.raw" 2>/dev/null || echo 0)
            if [[ "$size1" == "$size2" && "$size1" != "0" ]]; then
                break
            fi
        fi
    done

    # Kill LTspice (it doesn't exit on its own)
    kill "$pid" 2>/dev/null
    wait "$pid" 2>/dev/null || true

    if [[ -f "${base}.raw" ]]; then
        cp "${base}.raw" "$raw"
        # Extract time from .log if available
        if [[ -f "${base}.log" ]]; then
            grep -o 'Total elapsed time: [0-9.]*' "${base}.log" 2>/dev/null || true
        fi
        return 0
    fi
    return 1
}

# ── Convert netlist for Xyce ──────────────────────────────────────────
convert_for_xyce() {
    local net="$1" xyce_cir="$2" asc_dir="$3"

    # Read the netlist
    local content
    content=$(cat "$net")

    # Fix Latin-1 µ → u
    content=$(echo "$content" | sed 's/\xb5/u/g')

    # Remove .backanno
    content=$(echo "$content" | grep -iv '^\s*\.backanno')

    # Resolve .include paths — copy referenced files from LTspice lib
    local includes
    includes=$(echo "$content" | grep -i '^\s*\.include\s' || true)

    local inc_dir
    inc_dir=$(dirname "$xyce_cir")

    while IFS= read -r line; do
        [[ -z "$line" ]] && continue
        local fname
        fname=$(echo "$line" | sed 's/^\s*\.include\s\+//i' | tr -d '\r')

        # Try to find the include file
        local found=""
        # Check next to the .asc
        if [[ -f "$asc_dir/$fname" ]]; then
            found="$asc_dir/$fname"
        # Check LTspice lib/sub
        elif [[ -f "$LTSPICE_LIB/$fname" ]]; then
            found="$LTSPICE_LIB/$fname"
        # Check LTspice lib/cmp
        elif [[ -f "$LTSPICE_DIR/lib/cmp/$fname" ]]; then
            found="$LTSPICE_DIR/lib/cmp/$fname"
        fi

        if [[ -n "$found" ]]; then
            cp "$found" "$inc_dir/"
        fi
    done <<< "$includes"

    # Write the cleaned netlist
    echo "$content" > "$xyce_cir"

    # Add .PRINT if missing (Xyce requires it)
    if ! grep -qi '^\s*\.print' "$xyce_cir"; then
        local analysis=""
        analysis=$(grep -oi '^\s*\.\(tran\|ac\|dc\|op\|tf\)' "$xyce_cir" | head -1 | tr -d '.' | tr '[:lower:]' '[:upper:]' | tr -d '[:space:]')
        if [[ -n "$analysis" ]]; then
            # Insert .PRINT before .end
            sed -i "/^\s*\.end\s*$/i .PRINT ${analysis} FORMAT=RAW V(*)" "$xyce_cir"
        fi
    fi
}

# ── Xyce simulation ──────────────────────────────────────────────────
xyce_run() {
    local cir="$1" raw="$2"
    local raw_flag="-r"

    timeout "$TIMEOUT_XYCE" "$XYCE" "$raw_flag" "$raw" "$cir" 2>&1
}

# ── Process one .asc file ────────────────────────────────────────────
process_one() {
    local asc="$1"
    local basename
    basename=$(basename "$asc" .asc)
    local asc_dir
    asc_dir=$(dirname "$asc")

    local workdir="$WORK/$basename"
    rm -rf "$workdir"
    mkdir -p "$workdir"

    (( total++ ))
    echo "[$total] $basename"

    local notes=""
    local lt_status="SKIP" lt_time="-"
    local xy_status="SKIP" xy_time="-"

    # Step 1: Export netlist from LTspice
    local net="$workdir/${basename}.net"
    if ! ltspice_netlist "$asc" "$net" 2>/dev/null; then
        warn "$basename: LTspice netlist export failed"
        notes="netlist_export_failed"
        echo "$basename,$lt_status,$lt_time,$xy_status,$xy_time,$notes" >> "$RESULTS"
        (( skip++ ))
        return
    fi
    log "netlist exported ($(wc -l < "$net") lines)"

    # Step 2: Run LTspice simulation
    local lt_raw="$workdir/${basename}_lt.raw"
    local t_start t_end
    t_start=$(date +%s%N)
    if ltspice_run "$asc" "$lt_raw" 2>/dev/null; then
        t_end=$(date +%s%N)
        lt_time=$(echo "scale=3; ($t_end - $t_start) / 1000000000" | bc)
        lt_status="OK"
        (( ltspice_ok++ ))
        log "LTspice: OK (${lt_time}s, $(stat -c%s "$lt_raw") bytes)"
    else
        t_end=$(date +%s%N)
        lt_time=$(echo "scale=3; ($t_end - $t_start) / 1000000000" | bc)
        lt_status="FAIL"
        (( ltspice_fail++ ))
        log "LTspice: FAIL"
    fi

    # Step 3: Convert for Xyce
    local xyce_cir="$workdir/${basename}_xyce.cir"
    if ! convert_for_xyce "$net" "$xyce_cir" "$asc_dir" 2>/dev/null; then
        warn "$basename: conversion failed"
        notes="convert_failed"
        (( convert_fail++ ))
        echo "$basename,$lt_status,$lt_time,$xy_status,$xy_time,$notes" >> "$RESULTS"
        return
    fi

    # Step 4: Run Xyce
    local xy_raw="$workdir/${basename}_xyce.raw"
    local xy_log="$workdir/${basename}_xyce.log"
    t_start=$(date +%s%N)
    if xyce_run "$xyce_cir" "$xy_raw" > "$xy_log" 2>&1; then
        t_end=$(date +%s%N)
        xy_time=$(echo "scale=3; ($t_end - $t_start) / 1000000000" | bc)
        xy_status="OK"
        (( xyce_ok++ ))
        local raw_size=0
        [[ -f "$xy_raw" ]] && raw_size=$(stat -c%s "$xy_raw")
        log "Xyce:    OK (${xy_time}s, ${raw_size} bytes)"
    else
        t_end=$(date +%s%N)
        xy_time=$(echo "scale=3; ($t_end - $t_start) / 1000000000" | bc)
        xy_status="FAIL"
        (( xyce_fail++ ))
        # Capture error
        notes=$(tail -5 "$xy_log" 2>/dev/null | tr '\n' '|' | head -c 200)
        log "Xyce:    FAIL"
    fi

    echo "$basename,$lt_status,$lt_time,$xy_status,$xy_time,$notes" >> "$RESULTS"
}

# ── Main ──────────────────────────────────────────────────────────────

# Validate dependencies
[[ -x "$XYCE" ]] || { echo "ERROR: Xyce not found at $XYCE"; exit 1; }
[[ -f "$LTSPICE_EXE" ]] || { echo "ERROR: LTspice not found at $LTSPICE_EXE"; exit 1; }
command -v wine64 >/dev/null || { echo "ERROR: wine64 not found"; exit 1; }
command -v xvfb-run >/dev/null || { echo "ERROR: xvfb-run not found"; exit 1; }

# Determine input
INPUT="${1:-$LTSPICE_DIR/examples/Educational}"

echo "═══════════════════════════════════════════════════════════"
echo "  ltz compare: LTspice vs Xyce"
echo "  Input: $INPUT"
echo "  Work:  $WORK"
echo "═══════════════════════════════════════════════════════════"
echo ""

if [[ -f "$INPUT" && "$INPUT" == *.asc ]]; then
    # Single file
    process_one "$INPUT"
elif [[ -d "$INPUT" ]]; then
    # Directory of .asc files
    while IFS= read -r -d '' asc; do
        process_one "$asc"
    done < <(find "$INPUT" -maxdepth 1 -name "*.asc" -print0 | sort -z)
else
    echo "ERROR: $INPUT is not an .asc file or directory"
    exit 1
fi

# ── Summary ───────────────────────────────────────────────────────────
echo ""
echo "═══════════════════════════════════════════════════════════"
echo "  Results: $total circuits"
echo ""
echo "  LTspice:  $ltspice_ok OK / $ltspice_fail FAIL / $skip SKIP"
echo "  Xyce:     $xyce_ok OK / $xyce_fail FAIL / $convert_fail convert-fail"
echo ""
if (( total > 0 )); then
    echo "  Xyce pass rate: $(( xyce_ok * 100 / (total - skip) ))% (of attempted)"
fi
echo ""
echo "  Details: $RESULTS"
echo "═══════════════════════════════════════════════════════════"
