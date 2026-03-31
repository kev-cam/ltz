#!/bin/bash
#
# xyce_compat.sh — Test Xyce compatibility with LTspice examples
#
# Phase 1: Batch-export all .asc to .net via wine64 (one-time)
# Phase 2: Convert each .net → Xyce .cir and run through Xyce
#
# Usage:
#   ./tools/xyce_compat.sh                          # Educational examples
#   ./tools/xyce_compat.sh --export-only             # just export netlists
#   ./tools/xyce_compat.sh --xyce-only               # skip export, run Xyce
#   ./tools/xyce_compat.sh DIR                        # specific directory
#

set -uo pipefail

LTSPICE_DIR="/home/dkc/.wine/drive_c/Program Files/LTC/LTspiceXVII"
LTSPICE_EXE="$LTSPICE_DIR/XVIIx64.exe"
LTSPICE_LIBSUB="$LTSPICE_DIR/lib/sub"
LTSPICE_LIBCMP="$LTSPICE_DIR/lib/cmp"
XYCE="${XYCE:-/usr/local/src/Xyce-8/xyce-build/src/Xyce}"
WORK="/tmp/ltz_compat"
TIMEOUT_XYCE=30

# Parse args
export_only=0
xyce_only=0
INPUT=""
for arg in "$@"; do
    case "$arg" in
        --export-only) export_only=1 ;;
        --xyce-only)   xyce_only=1 ;;
        *)             INPUT="$arg" ;;
    esac
done
INPUT="${INPUT:-$LTSPICE_DIR/examples/Educational}"

mkdir -p "$WORK"

# ── Phase 1: Batch export netlists ────────────────────────────────────

export_netlists() {
    echo "═══ Phase 1: Exporting LTspice netlists ═══"
    local count=0 ok=0 fail=0

    while IFS= read -r -d '' asc; do
        (( count++ ))
        local base="${asc%.asc}"
        local name
        name=$(basename "$asc" .asc)

        # Skip if .net already exists and is newer than .asc
        if [[ -f "${base}.net" && "${base}.net" -nt "$asc" ]]; then
            (( ok++ ))
            continue
        fi

        # Remove stale .net
        rm -f "${base}.net"

        # Export via wine64
        local win_path
        win_path=$(winepath -w "$asc" 2>/dev/null) || win_path="$asc"

        if timeout 15 xvfb-run -a wine64 "$LTSPICE_EXE" -netlist "$win_path" \
                >/dev/null 2>&1; then
            if [[ -f "${base}.net" ]]; then
                (( ok++ ))
                echo "  ✓ $name"
            else
                (( fail++ ))
                echo "  ✗ $name (no .net produced)"
            fi
        else
            (( fail++ ))
            echo "  ✗ $name (wine64 failed)"
        fi
    done < <(find "$INPUT" -maxdepth 1 -name "*.asc" -print0 | sort -z)

    echo ""
    echo "  Exported: $ok / $count    Failed: $fail"
    echo ""
}

# ── Phase 2: Convert and run through Xyce ─────────────────────────────

convert_netlist() {
    # Convert a single .net → Xyce-compatible .cir via Python converter
    local net="$1" outdir="$2" asc_dir="$3"
    local script_dir
    script_dir="$(dirname "$0")"

    python3 "$script_dir/ltspice2xyce.py" "$net" -o "$outdir" -d "$asc_dir" \
        >/dev/null 2>&1

    local name
    name=$(basename "$net" .net)
    echo "$outdir/${name}.cir"
}

run_xyce_tests() {
    echo "═══ Phase 2: Running through Xyce ═══"
    local count=0 ok=0 fail=0 skip=0
    local results="$WORK/results.csv"
    echo "name,status,time_s,xyce_exit,error" > "$results"

    while IFS= read -r -d '' asc; do
        local base="${asc%.asc}"
        local name
        name=$(basename "$asc" .asc)
        local net="${base}.net"

        (( count++ ))

        # Need a .net file
        if [[ ! -f "$net" ]]; then
            echo "  - $name (no .net, skipped)"
            echo "$name,SKIP,0,0,no_netlist" >> "$results"
            (( skip++ ))
            continue
        fi

        # Prepare work directory
        local workdir="$WORK/$name"
        rm -rf "$workdir"
        mkdir -p "$workdir"

        # Convert
        local asc_dir
        asc_dir=$(dirname "$asc")
        local cir
        cir=$(convert_netlist "$net" "$workdir" "$asc_dir" 2>/dev/null)

        if [[ ! -f "$cir" ]]; then
            echo "  ✗ $name (convert failed)"
            echo "$name,CONVERT_FAIL,0,0,convert_failed" >> "$results"
            (( fail++ ))
            continue
        fi

        # Run Xyce
        local raw="$workdir/${name}.raw"
        local log="$workdir/${name}.log"
        local t_start t_end elapsed
        t_start=$(date +%s%N)
        timeout "$TIMEOUT_XYCE" "$XYCE" -r "$raw" "$cir" > "$log" 2>&1
        local xyce_exit=$?
        t_end=$(date +%s%N)
        elapsed=$(echo "scale=3; ($t_end - $t_start) / 1000000000" | bc)

        if [[ $xyce_exit -eq 0 && -f "$raw" ]]; then
            local raw_size
            raw_size=$(stat -c%s "$raw")
            (( ok++ ))
            echo "  ✓ $name (${elapsed}s, ${raw_size}B)"
            echo "$name,OK,$elapsed,$xyce_exit," >> "$results"
        else
            (( fail++ ))
            local err
            err=$(grep -i 'error\|fatal\|Netlist error' "$log" 2>/dev/null | head -3 | tr '\n' '|' | head -c 200)
            echo "  ✗ $name (${elapsed}s, exit=$xyce_exit)"
            echo "$name,FAIL,$elapsed,$xyce_exit,$err" >> "$results"
        fi
    done < <(find "$INPUT" -maxdepth 1 -name "*.asc" -print0 | sort -z)

    echo ""
    echo "═══════════════════════════════════════════════════════════"
    echo "  Total:   $count"
    echo "  Xyce OK: $ok    FAIL: $fail    SKIP: $skip"
    if (( count - skip > 0 )); then
        echo "  Pass rate: $(( ok * 100 / (count - skip) ))%"
    fi
    echo ""
    echo "  Results: $results"
    echo "  Work:    $WORK/"

    # Show failures summary
    if (( fail > 0 )); then
        echo ""
        echo "  Failed circuits:"
        grep ',FAIL,' "$results" | while IFS=, read -r fname status time_s exit_code error; do
            echo "    $fname: $error"
        done
    fi
    echo "═══════════════════════════════════════════════════════════"
}

# ── Main ──────────────────────────────────────────────────────────────

[[ -x "$XYCE" ]] || { echo "ERROR: Xyce not found at $XYCE"; exit 1; }

echo ""
echo "  ltz Xyce compatibility test"
echo "  Input: $INPUT"
echo "  Xyce:  $XYCE"
echo ""

if (( xyce_only == 0 )); then
    export_netlists
fi

if (( export_only == 0 )); then
    run_xyce_tests
fi
