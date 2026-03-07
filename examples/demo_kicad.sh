#!/bin/bash
#
# demo_kicad.sh — Open the RC filter schematic in KiCad eeschema
#                 using the Xyce-backed ngspice shim.
#
# Usage:
#   ./examples/demo_kicad.sh
#
# Once eeschema opens:
#   1. Inspect → Simulator (or Tools → Simulator)
#   2. Click "Run/Stop Simulation" (play button)
#   3. Click signal names in the list to add traces
#
# The simulation runs through Xyce instead of ngspice.
#

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
LTZ_ROOT="$(dirname "$SCRIPT_DIR")"
SCHEMATIC="$SCRIPT_DIR/rc_filter/rc_filter.kicad_sch"

if [ ! -f "$SCHEMATIC" ]; then
    echo "Error: schematic not found at $SCHEMATIC" >&2
    exit 1
fi

# Use ltz-kicad launcher for the shim setup
exec "$LTZ_ROOT/bin/ltz-kicad" --eeschema "$SCHEMATIC"
