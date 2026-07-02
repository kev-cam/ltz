#!/usr/bin/env bash
#
# pack-xyce.sh -- bundle the self-contained /opt/sims Xyce engines into a
# portable artifact for `bootstrap.ps1 -XyceArtifacts` (Xyce has no apt package,
# so this tarball is how a fresh box gets the prebuilt engines). ngspice is NOT
# packed -- it comes from apt on the target.
#
#   bash pack-xyce.sh [out.tar.gz]
# default out: /mnt/c/cygwin64/usr/local/sims-artifacts/sims-xyce.tar.gz
set -euo pipefail
export PATH=/usr/bin:/bin:/usr/local/bin

# default lands on the Cygwin/Windows side (survives a WSL unregister); the real
# WSL /usr/local is root-owned, so don't write there.
OUT="${1:-/mnt/c/cygwin64/home/Claude/sims-artifacts/sims-xyce.tar.gz}"
mkdir -p "$(dirname "$OUT")"
cd /opt/sims

engines=""
for e in xyce xyce-mpi; do [ -d "$e" ] && engines="$engines $e"; done
[ -n "$engines" ] || { echo "pack-xyce: no xyce engines under /opt/sims" >&2; exit 1; }

# -czf preserves the relative 'current' symlinks so they restore intact
tar -czf "$OUT" $engines
echo "pack-xyce: packed$engines -> $OUT ($(du -h "$OUT" | cut -f1))"
