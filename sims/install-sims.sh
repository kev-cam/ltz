#!/usr/bin/env bash
#
# install-sims.sh -- populate /opt/sims with self-contained, prebuilt SPICE
# engines so a single selector can funnel QSPICE decks to any of them.
#
#   /opt/sims/
#     xyce/<ver>/{bin/Xyce, lib/*.so, run}     xyce/current -> <ver>
#     xyce-mpi/<ver>/{bin/Xyce, lib/*.so, run}  xyce-mpi/current -> <ver>
#     ngspice/<ver>/{bin/ngspice, run}          ngspice/current -> <ver>
#     sim                                       dispatcher: sim <engine[@ver]> ...
#
# Each engine is self-contained: its run launcher pins LD_LIBRARY_PATH to its
# own lib/, so the serial and MPI Trilinos sets (identical SONAMEs, different
# ABIs) never shadow each other. Binaries are copied from the existing builds
# (prebuilt -- nothing is recompiled here). Reversible: rm -rf /opt/sims.
#
# Run inside WSL:  bash /mnt/c/cygwin64/usr/local/src/ltz/sims/install-sims.sh
# Needs sudo once to create/chown /opt/sims (password via $SUDO_PW or prompt).
set -u
export PATH=/usr/bin:/bin:/usr/local/bin

# --- sources (override via env if builds move) -------------------------------
XY_SER_BIN=${XY_SER_BIN:-/usr/local/src/xyce-build/src/Xyce}
XY_SER_LIB=${XY_SER_LIB:-/usr/local/src/xyce-build/src}      # holds libXyceLib.so
XY_MPI_BIN=${XY_MPI_BIN:-/home/claude/xyce-build-mpi/src/Xyce}
XY_MPI_LIB=${XY_MPI_LIB:-/home/claude/trilinos-mpi/lib}
NG_BIN=${NG_BIN:-/usr/bin/ngspice}
NG_CM_DIR=${NG_CM_DIR:-/usr/lib/x86_64-linux-gnu/ngspice}   # XSPICE codemodels (*.cm)
NG_SHARE=${NG_SHARE:-/usr/share/ngspice}                    # spinit + scripts

XY_VER=${XY_VER:-20260518}     # DEVELOPMENT-202605180000 tag
NG_VER=${NG_VER:-45.2}

ROOT=/opt/sims
# Only used when creating /opt/sims as a non-root user. Set SUDO_PW to feed a
# password non-interactively; otherwise sudo prompts (or is passwordless).
SUDO_PW=${SUDO_PW:-}
sudo_do() { if [ -n "$SUDO_PW" ]; then echo "$SUDO_PW" | sudo -S -p '' "$@"; else sudo "$@"; fi; }

say() { echo "[install-sims] $*"; }
die() { echo "[install-sims] FAIL: $*" >&2; exit 1; }

# --- create /opt/sims owned by us -------------------------------------------
if [ ! -d "$ROOT" ]; then
  say "creating $ROOT (sudo)"
  sudo_do mkdir -p "$ROOT" || die "mkdir $ROOT"
  sudo_do chown "$(id -un):$(id -gn)" "$ROOT" || die "chown $ROOT"
fi

# copy the dynamic-dep closure of a binary, restricted to a path pattern,
# into dest/ using each lib's basename (== its SONAME for these libs).
copy_closure() {
  local bin="$1" ldpath="$2" keep="$3" dest="$4"
  LD_LIBRARY_PATH="$ldpath" ldd "$bin" 2>/dev/null \
    | awk -v p="$keep" '/=>/ && $3 ~ p {print $3}' \
    | while read -r so; do
        [ -f "$so" ] && cp -Lf "$so" "$dest/$(basename "$so")"
      done
}

install_engine() {  # name ver
  local name="$1" ver="$2" d="$ROOT/$1/$2"
  mkdir -p "$d/bin" "$d/lib" || die "mkdir $d"
  echo "$d"
}

link_current() {  # name ver
  ln -sfn "$2" "$ROOT/$1/current"
}

# --- xyce (serial) -----------------------------------------------------------
if [ -x "$XY_SER_BIN" ]; then
  say "xyce (serial) $XY_VER"
  d=$(install_engine xyce "$XY_VER")
  cp -Lf "$XY_SER_BIN" "$d/bin/Xyce"
  cp -Lf "$XY_SER_LIB/libXyceLib.so" "$d/lib/" 2>/dev/null
  copy_closure "$XY_SER_BIN" "$XY_SER_LIB" '^/usr/local/' "$d/lib"
  cat > "$d/run" <<'RUN'
#!/bin/sh
d=$(cd "$(dirname "$0")" && pwd)
export LD_LIBRARY_PATH="$d/lib${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"
exec "$d/bin/Xyce" "$@"
RUN
  chmod +x "$d/run"
  link_current xyce "$XY_VER"
  say "  $(ls "$d/lib" | wc -l) libs, binary $(du -h "$d/bin/Xyce" | cut -f1)"
else
  say "skip xyce (serial): $XY_SER_BIN not found"
fi

# --- xyce-mpi ----------------------------------------------------------------
if [ -x "$XY_MPI_BIN" ]; then
  say "xyce-mpi $XY_VER"
  d=$(install_engine xyce-mpi "$XY_VER")
  cp -Lf "$XY_MPI_BIN" "$d/bin/Xyce"
  copy_closure "$XY_MPI_BIN" "$XY_MPI_LIB" 'trilinos-mpi' "$d/lib"
  cat > "$d/run" <<'RUN'
#!/bin/sh
# NP=<n> overrides rank count (default 4). System OpenMPI provides mpirun/libmpi.
d=$(cd "$(dirname "$0")" && pwd)
export LD_LIBRARY_PATH="$d/lib${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"
exec mpirun --allow-run-as-root --oversubscribe -np "${NP:-4}" "$d/bin/Xyce" "$@"
RUN
  chmod +x "$d/run"
  link_current xyce-mpi "$XY_VER"
  say "  $(ls "$d/lib" | wc -l) libs, binary $(du -h "$d/bin/Xyce" | cut -f1)"
else
  say "skip xyce-mpi: $XY_MPI_BIN not found"
fi

# --- ngspice (apt prebuilt binary + bundled codemodels/spinit) ---------------
# The bare binary parasitically reaches into /usr for spinit and the XSPICE
# *.cm codemodels (spinit loads them by absolute path). Bundle that ngspice-
# specific runtime here -- parity with the self-contained xyce libs -- and
# point the launcher's SPICE_LIB_DIR at it. (Base .so deps: readline/X11/libc
# stay system, same as xyce's libstdc++/BLAS.)
if [ -x "$NG_BIN" ]; then
  say "ngspice $NG_VER"
  d=$(install_engine ngspice "$NG_VER")
  cp -Lf "$NG_BIN" "$d/bin/ngspice"
  mkdir -p "$d/lib/ngspice" "$d/share"
  cp -Lf "$NG_CM_DIR"/*.cm "$d/lib/ngspice/" 2>/dev/null
  cp -a "$NG_SHARE" "$d/share/" 2>/dev/null     # dest exists -> $d/share/ngspice/{scripts/spinit,...}
  # repoint spinit's codemodel directives at the bundled .cm
  if [ -f "$d/share/ngspice/scripts/spinit" ]; then
    sed -i "s#$NG_CM_DIR#$d/lib/ngspice#g" "$d/share/ngspice/scripts/spinit"
  fi
  cat > "$d/run" <<RUN
#!/bin/sh
d=\$(cd "\$(dirname "\$0")" && pwd)
export SPICE_LIB_DIR="\$d/share/ngspice"        # spinit + codemodels resolve here
exec "\$d/bin/ngspice" "\$@"
RUN
  chmod +x "$d/run"
  link_current ngspice "$NG_VER"
  say "  binary $(du -h "$d/bin/ngspice" | cut -f1), $(ls "$d/lib/ngspice" 2>/dev/null | wc -l) codemodels, spinit bundled"
else
  say "skip ngspice: $NG_BIN not found"
fi

# --- the sim dispatcher ------------------------------------------------------
cat > "$ROOT/sim" <<'SIM'
#!/bin/sh
# sim <engine[@version]> [args...]   run an engine from /opt/sims
# sim --list                         show installed engines/versions
root=/opt/sims
if [ -z "${1:-}" ] || [ "$1" = "--list" ] || [ "$1" = "-l" ]; then
  for e in "$root"/*/; do
    en=$(basename "$e"); [ -d "$e" ] || continue
    cur=$(readlink "$e/current" 2>/dev/null)
    vers=$(ls -d "$e"*/ 2>/dev/null | while read -r v; do basename "$v"; done \
           | grep -v '^current$' | tr '\n' ' ')
    [ -n "$vers" ] && printf '%-12s current=%-10s versions: %s\n' "$en" "${cur:-?}" "$vers"
  done
  exit 0
fi
spec="$1"; shift
eng="${spec%@*}"; ver="${spec##*@}"; [ "$ver" = "$spec" ] && ver=current
run="$root/$eng/$ver/run"
[ -x "$run" ] || { echo "sim: no engine '$eng' version '$ver' ($run)" >&2; exit 127; }
exec "$run" "$@"
SIM
chmod +x "$ROOT/sim"

# --- the sims manager (subcommands) + a default manifest ---------------------
REPO_DIR=$(cd "$(dirname "$0")" && pwd)
[ -f "$REPO_DIR/sims" ] && { cp -f "$REPO_DIR/sims" "$ROOT/sims"; chmod +x "$ROOT/sims"; }
[ -f "$REPO_DIR/sims-autoupdate.sh" ] && { cp -f "$REPO_DIR/sims-autoupdate.sh" "$ROOT/sims-autoupdate.sh"; chmod +x "$ROOT/sims-autoupdate.sh"; }
if [ ! -f "$ROOT/sims.conf" ]; then
  cat > "$ROOT/sims.conf" <<'CONF'
# sims.conf -- desired environment; `sims sync` reconciles the install to this.
# Fork per box. Lines: `engine <name> <version|current>` or `extra <name>`.
engine xyce     current
engine xyce-mpi current
engine ngspice  current
# extra glayout
# extra kestrel
CONF
fi

say "done. /opt/sims contents:"
"$ROOT/sim" --list
