#!/usr/bin/env bash
#
# bootstrap-wsl.sh -- provision a fresh Ubuntu/WSL for the /opt/sims funnel.
# Run AS ROOT inside WSL (bootstrap.ps1 does this):
#   bash bootstrap-wsl.sh [workuser] [with_mingw:0|1] [artifacts_dir]
# artifacts_dir holds the *.tar.gz from pack-artifacts.sh (engines, OpenVAF,
# sky130 PDK, ssh keys); restored so a teardown loses nothing. FORCE_RESTORE=1
# overwrites targets that already exist.
#
# Installs the exact apt dependency set (mapped from the engines' ldd closure),
# creates the work user, links /usr/local/src to the Cygwin repo tree, optionally
# unpacks prebuilt Xyce engine artifacts, then runs install-sims.sh to populate
# /opt/sims. ngspice's prebuilt binary+codemodels come from the apt package and
# are bundled into /opt by install-sims.sh.
set -euo pipefail
export PATH=/usr/sbin:/usr/bin:/sbin:/bin:/usr/local/sbin:/usr/local/bin   # sbin for useradd/usermod/chpasswd/ln
export DEBIAN_FRONTEND=noninteractive

WORKUSER="${1:-claude}"
WITH_MINGW="${2:-0}"
ARTIFACTS_DIR="${3:-}"             # optional dir of *.tar.gz: sims-xyce, openvaf, pdk-sky130, wsl-ssh-backup, [xyce-build-mpi]
EXTRAS="${4:-}"                    # optional csv of extras to install after: glayout,devtools,kestrel
SRC_WIN="/mnt/c/cygwin64/usr/local/src"   # Cygwin-side repo tree, as seen from WSL
WORKPW="${WORKPW:-}"                       # set to give the work user a password; else passwordless sudo

say() { echo "[bootstrap-wsl] $*"; }
die() { echo "[bootstrap-wsl] FAIL: $*" >&2; exit 1; }
[ "$(id -u)" = 0 ] || die "must run as root (bootstrap.ps1 invokes 'wsl -u root')"

# --- apt dependency set ------------------------------------------------------
# base + perl (the bridge's translators), Xyce serial runtime, OpenMPI (mpirun +
# the whole transport stack via openmpi-bin), ngspice (X11/readline deps + the
# prebuilt binary/codemodels install-sims bundles). Verified to resolve on 26.04.
PKGS="ca-certificates make perl
  libblas3 liblapack3 libgfortran5 libfftw3-double3 libamd3
  libsuitesparseconfig7 libyaml-cpp0.8 libgomp1
  openmpi-bin
  ngspice"
[ "$WITH_MINGW" = 1 ] && PKGS="$PKGS gcc-mingw-w64-x86-64"   # only if rebuilding qspice-shim.exe

say "apt update + install"
apt-get update -qq
# shellcheck disable=SC2086
apt-get install -y --no-install-recommends $PKGS

# --- work user ---------------------------------------------------------------
if ! id -u "$WORKUSER" >/dev/null 2>&1; then
  say "creating user '$WORKUSER'"
  useradd -m -s /bin/bash "$WORKUSER"
  usermod -aG sudo "$WORKUSER"
  if [ -n "$WORKPW" ]; then
    echo "$WORKUSER:$WORKPW" | chpasswd
  else
    # no password supplied: passwordless sudo (typical single-user WSL dev box)
    printf '%s ALL=(ALL) NOPASSWD:ALL\n' "$WORKUSER" > "/etc/sudoers.d/90-$WORKUSER"
    chmod 440 "/etc/sudoers.d/90-$WORKUSER"
  fi
fi
# make this the distro's default login user (so plain `wsl` lands as them)
if ! grep -q '^\[user\]' /etc/wsl.conf 2>/dev/null; then
  printf '[user]\ndefault=%s\n' "$WORKUSER" >> /etc/wsl.conf
fi

# --- /usr/local/src -> Cygwin tree (so absolute paths resolve in both worlds) -
if [ ! -e "$SRC_WIN" ]; then
  say "WARN: $SRC_WIN not visible -- is the Cygwin tree on this machine? skipping symlink"
else
  if [ -L /usr/local/src ] || [ ! -e /usr/local/src ]; then
    ln -sfn "$SRC_WIN" /usr/local/src; say "/usr/local/src -> $SRC_WIN"
  elif [ -d /usr/local/src ] && [ -z "$(ls -A /usr/local/src 2>/dev/null)" ]; then
    rmdir /usr/local/src && ln -sfn "$SRC_WIN" /usr/local/src; say "/usr/local/src (was empty) -> $SRC_WIN"
  elif [ "$(readlink -f /usr/local/src)" != "$(readlink -f "$SRC_WIN")" ]; then
    say "WARN: /usr/local/src exists and is non-empty; leaving as-is"
  fi
fi

# --- pre-create /opt/sims owned by the work user -----------------------------
mkdir -p /opt/sims
chown "$WORKUSER:$WORKUSER" /opt/sims

# --- restore portable artifacts (engines, OpenVAF, sky130 PDK, ssh keys) -----
# These hold the non-apt, non-repo WSL state that a teardown would otherwise
# lose. apt (above) + the Cygwin /usr/local/src tree cover everything else, so
# bootstrap + these artifacts == the full environment. OpenVAF is a custom build
# and the PDK install has py3.14/no-pip/zstd gotchas, so we restore, not refetch.
if [ -n "$ARTIFACTS_DIR" ] && [ ! -d "$ARTIFACTS_DIR" ]; then
  say "WARN: artifacts dir $ARTIFACTS_DIR not found; skipping restore"
  ARTIFACTS_DIR=""
fi
if [ -n "$ARTIFACTS_DIR" ]; then
  say "restoring artifacts from $ARTIFACTS_DIR"
  uhome=$(getent passwd "$WORKUSER" | cut -d: -f6)
  unpack() {  # tarball into_dir sentinel
    local tb="$ARTIFACTS_DIR/$1" into="$2" sent="$3"
    [ -f "$tb" ] || { say "  ${1}: absent, skip"; return 0; }
    if [ -e "$sent" ] && [ "${FORCE_RESTORE:-0}" != 1 ]; then say "  ${1}: target present, skip (FORCE_RESTORE=1 to overwrite)"; return 0; fi
    mkdir -p "$into"; tar -xzf "$tb" -C "$into"; say "  restored ${1} -> ${into}"
  }
  unpack sims-xyce.tar.gz      /opt/sims  /opt/sims/xyce/current     # serial + MPI engines
  unpack openvaf.tar.gz        /opt       /opt/openvaf/openvaf       # custom OpenVAF 23.5.0
  unpack pdk-sky130.tar.gz     /opt       /opt/pdk/sky130A           # sky130A + sky130B
  unpack xyce-build-mpi.tar.gz "$uhome"   "$uhome/xyce-build-mpi"    # MPI build tree (rebuild capability; optional)
  unpack wsl-ssh-backup.tar.gz "$uhome"   "$uhome/.ssh/id_rsa"       # unrecoverable private key + .gitconfig
  # ownership + ssh perms
  chown -R "$WORKUSER:$WORKUSER" /opt/sims "$uhome" 2>/dev/null || true
  for d in /opt/openvaf /opt/pdk; do [ -d "$d" ] && chown -R "$WORKUSER:$WORKUSER" "$d"; done
  if [ -d "$uhome/.ssh" ]; then
    chmod 700 "$uhome/.ssh"; chmod 600 "$uhome/.ssh/"* 2>/dev/null || true
    chmod 644 "$uhome/.ssh/"*.pub 2>/dev/null || true
  fi
fi

# --- populate /opt/sims ------------------------------------------------------
SIMS_INSTALLER=/usr/local/src/ltz/sims/install-sims.sh
[ -f "$SIMS_INSTALLER" ] || die "$SIMS_INSTALLER not found (repo tree mounted?)"
say "running install-sims.sh as $WORKUSER"
sudo -u "$WORKUSER" bash "$SIMS_INSTALLER"

# optional extras (run as root so apt-based ones work; glayout restore -> chown)
if [ -n "$EXTRAS" ]; then
  say "installing extras: $EXTRAS"
  SIMS_ARTIFACTS="$ARTIFACTS_DIR" /opt/sims/sims extras ${EXTRAS//,/ } || true
  [ -d /opt/glayout-env ] && chown -R "$WORKUSER:$WORKUSER" /opt/glayout-env 2>/dev/null
fi

say "done."
sudo -u "$WORKUSER" /opt/sims/sims doctor || true
# flag the common fresh-box gap
for e in xyce xyce-mpi; do
  [ -x "/opt/sims/$e/current/run" ] || say "NOTE: $e not installed -- supply prebuilt Xyce via the artifacts arg, or build it (no apt package exists)."
done
