#!/usr/bin/env bash
#
# pack-artifacts.sh -- capture all the non-apt, non-repo WSL state into portable
# artifacts, so a teardown + bootstrap reconstructs the environment faithfully.
# Run on the golden box (WSL). Restore is done by bootstrap-wsl.sh -ArtifactsDir.
#
# Why artifacts and not re-fetch: OpenVAF here is a custom local build (no public
# download matches it) and the sky130 PDK install has py3.14/no-pip/zstd gotchas.
# apt packages + the Cygwin /usr/local/src repo tree are NOT packed (reproducible).
#
#   bash pack-artifacts.sh [artifacts_dir]
#   WITH_BUILD_TREES=1 bash pack-artifacts.sh   # also pack the 1.5G MPI build tree
set -euo pipefail
export PATH=/usr/bin:/bin:/usr/local/bin

ART="${1:-/mnt/c/cygwin64/home/Claude/sims-artifacts}"
WITH_BUILD_TREES="${WITH_BUILD_TREES:-0}"
mkdir -p "$ART"

pack() {  # label base item...
  local label="$1" out="$ART/$1.tar.gz" base="$2"; shift 2
  ( cd "$base" && tar -czf "$out" "$@" )
  echo "  $label.tar.gz  $(du -h "$out" | cut -f1)  [$base: $*]"
}

echo "pack-artifacts -> $ART"
[ -d /opt/sims/xyce ] && pack sims-xyce    /opt/sims xyce xyce-mpi    # runnable engines (self-contained)
[ -d /opt/openvaf ]   && pack openvaf      /opt      openvaf          # custom OpenVAF 23.5.0 build
[ -d /opt/pdk ]       && pack pdk-sky130   /opt      pdk              # sky130A + sky130B
[ -d /opt/glayout-env ] && pack glayout-env /opt    glayout-env      # extra: glayout (972M) -- closes the teardown gap
[ -d /opt/kestrel ]   && pack kestrel      /opt      kestrel          # extra: kestrel (PLL design), once sourced
# ssh keys + git identity (unrecoverable private key)
if [ -d "$HOME/.ssh" ]; then
  gc=""; [ -f "$HOME/.gitconfig" ] && gc=".gitconfig"
  pack wsl-ssh-backup "$HOME" .ssh $gc
fi
# optional: the MPI build TREE (only needed to rebuild/relink, not to run)
if [ "$WITH_BUILD_TREES" = 1 ] && [ -d "$HOME/xyce-build-mpi" ]; then
  pack xyce-build-mpi "$HOME" xyce-build-mpi trilinos-mpi
fi

echo "done. artifacts:"
ls -lh "$ART"/*.tar.gz 2>/dev/null | awk '{print "  "$NF" "$5}'
