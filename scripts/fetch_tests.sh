#!/usr/bin/env bash
#
# fetch_tests.sh — Populate ../ltz-tests with LTspice community circuits
#
# Run from the ltz repo root:
#   ./scripts/fetch_tests.sh
#
# Creates a sibling directory ../ltz-tests/ containing cloned repos
# and a summary of available test material.
#

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
TEST_DIR="$(cd "$REPO_ROOT/.." && pwd)/ltz-tests"

echo "═══════════════════════════════════════════════════════════"
echo "  ltz test corpus fetcher"
echo "  Target: $TEST_DIR"
echo "═══════════════════════════════════════════════════════════"
echo ""

mkdir -p "$TEST_DIR"

# ─── Repository definitions ───────────────────────────────────
# Format: directory_name|git_url|description
REPOS=(
    "circuits-ltspice|https://github.com/mick001/Circuits-LTSpice.git|Educational circuits collection (131 .asc)"
    "spice-libraries|https://github.com/dnemec/SPICE-Libraries.git|Community models salvaged from Yahoo groups"
    "ecircuit|https://github.com/JesseHardingMurillo/LTspice_Ecircuit.git|eCircuit Center examples (60 .cir netlists + models)"
    "powersim|https://github.com/kosokno/LTspicePowerSim.git|Power electronics: converters, PFC, motor drivers (102 .asc)"
    "jaymac-circuits|https://github.com/Mr-Jaymac/LTSpice-Circuits.git|Basic circuits: rectifiers, oscillators, op-amps (14 .asc)"
    "three-phase-inverter|https://github.com/mrjacopong/Three_phase_inverter_LTspice.git|Three-phase VSI simulation"
    "dhaffner-schematics|https://github.com/dhaffnersr/LTSpice-.ASC-schematics.git|Assorted LTspice designs and simulations"
)

# ─── Clone or update each repo ────────────────────────────────
for entry in "${REPOS[@]}"; do
    IFS='|' read -r dirname url desc <<< "$entry"
    target="$TEST_DIR/$dirname"

    echo "── $dirname"
    echo "   $desc"

    if [ -d "$target/.git" ]; then
        echo "   Updating..."
        (cd "$target" && git pull --ff-only 2>&1 | sed 's/^/   /')
    else
        echo "   Cloning..."
        git clone --depth 1 "$url" "$target" 2>&1 | sed 's/^/   /'
    fi
    echo ""
done

# ─── Scan and report ──────────────────────────────────────────
echo "═══════════════════════════════════════════════════════════"
echo "  Test corpus summary"
echo "═══════════════════════════════════════════════════════════"
echo ""

total_asc=0
total_cir=0
total_sub=0

for entry in "${REPOS[@]}"; do
    IFS='|' read -r dirname url desc <<< "$entry"
    target="$TEST_DIR/$dirname"

    asc_count=$(find "$target" -name '*.asc' 2>/dev/null | wc -l)
    cir_count=$(find "$target" \( -name '*.cir' -o -name '*.net' -o -name '*.sp' \) 2>/dev/null | wc -l)
    sub_count=$(find "$target" \( -name '*.sub' -o -name '*.lib' -o -name '*.mod' \) 2>/dev/null | wc -l)

    total_asc=$((total_asc + asc_count))
    total_cir=$((total_cir + cir_count))
    total_sub=$((total_sub + sub_count))

    printf "  %-25s  %4d .asc  %4d .cir  %4d models\n" "$dirname" "$asc_count" "$cir_count" "$sub_count"
done

echo "  ─────────────────────────────────────────────────────"
printf "  %-25s  %4d .asc  %4d .cir  %4d models\n" "TOTAL" "$total_asc" "$total_cir" "$total_sub"
echo ""

# ─── Run ltz_convert scan if available ─────────────────────────
CONVERTER="$REPO_ROOT/tools/ltz_convert.py"
if [ -f "$CONVERTER" ]; then
    echo "═══════════════════════════════════════════════════════════"
    echo "  Xyce compatibility scan (self-contained .cir files)"
    echo "═══════════════════════════════════════════════════════════"
    echo ""

    for entry in "${REPOS[@]}"; do
        IFS='|' read -r dirname url desc <<< "$entry"
        target="$TEST_DIR/$dirname"
        cir_count=$(find "$target" -name '*.cir' 2>/dev/null | wc -l)

        if [ "$cir_count" -gt 0 ]; then
            echo "── $dirname ($cir_count .cir files)"
            python3 "$CONVERTER" --scan --self-contained "$target" 2>&1 | tail -5
            echo ""
        fi
    done
fi

echo "═══════════════════════════════════════════════════════════"
echo "  Done. Test corpus is at: $TEST_DIR"
echo ""
echo "  Next steps:"
echo "    python3 tools/ltz_convert.py --scan ../ltz-tests/ecircuit/"
echo "    python3 tools/ltz_convert.py --batch --self-contained ../ltz-tests/ecircuit/ -o tests/converted/"
echo "    Xyce tests/converted/00_RC_LOW_PASS_FILTER/Lpfilter1.cir"
echo "═══════════════════════════════════════════════════════════"
