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
SIM=/opt/sims/sim          # engine launcher: sim <engine[@ver]> ...
ENGINE=xyce                # default target; overridden by engine= in args file
OPENVAF=${OPENVAF:-/opt/openvaf/openvaf}                      # Verilog-A -> OSDI (ngspice)
VA_PATH=${VA_PATH:-/usr/local/src/xyce/utils/qspice_va}      # fallback .va search root
LOG="$QSHIM_DIR/bridge.log"

log() { echo "[$(date '+%F %T')] $*" >> "$LOG"; }
die() { log "FAIL: $*"; exit 1; }

log "args: $*"

# --pid N: the shim wrote the original Windows cmdline tail (quotes, spaces,
# backslashes intact) to qshim-args/N.txt -- parse it ourselves, since none
# of that survives wsl.exe argv marshaling.
if [ "$1" = "--pid" ] && [ -n "$2" ]; then
  argfile="/mnt/c/cygwin64/tmp/qshim-args/$2.txt"
  [ -f "$argfile" ] || die "args file missing: $argfile"
  e=$(sed -n 's/^engine=//p' "$argfile" | head -1); [ -n "$e" ] && ENGINE=$e
  mapfile -d '' -t QARGS < <(perl -e '
    open my $f, "<", $ARGV[0] or exit 1;
    my $l = <$f>; $l =~ s/[\r\n]+$//;
    while ($l =~ /\G\s*(?:"([^"]*)"|(\S+))/gc) {
        my $t = defined $1 ? $1 : $2;
        print $t, "\0";
    }' "$argfile")
  log "parsed ${#QARGS[@]} args from $argfile: ${QARGS[*]}"
  set -- "${QARGS[@]}"
fi

# Observed GUI protocol (shim.log): QUX runs the engine as
#   -pipe -QUX -Op "<schematic.qsch>" -r "<tmp.N.qraw>" -hWnd=<n>
# with the NETLIST PIPED ON STDIN; -Op names the origin schematic (its dir
# anchors relative includes), -hWnd is the GUI progress handle (ignored).
deck=""; qraw=""; out=""; op_qsch=""; op_orig=""
while [ $# -gt 0 ]; do
  case "$1" in
    -r)      qraw="$2"; shift 2 ;;
    -o)      out="$2";  shift 2 ;;
    -Op)     op_qsch="$2"; op_orig="$2"; shift 2 ;;
    -binary|-ascii|-pipe|-QUX) shift ;;
    -hWnd=*) shift ;;
    -simProcessID|-viewer) log "ignoring $1 $2"; shift 2 ;;   # viewer handshake
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

# run where the schematic lives so its relative includes resolve
rundir=$(dirname "$deck")
if [ -n "$op_qsch" ]; then
  case "$op_qsch" in [A-Za-z]:*|*\\*) op_qsch=$(wslpath -u "$op_qsch" 2>/dev/null);; esac
  [ -n "$op_qsch" ] && [ -d "$(dirname "$op_qsch")" ] && rundir=$(dirname "$op_qsch")
fi

log "engine=$ENGINE"
case "$ENGINE" in
  xyce|xyce-mpi|xyce@*|xyce-mpi@*)
    perl "$Q2X" -o "$xdeck" "$deck" >> "$LOG" 2>&1 || die "qspice2xyce"
    ( cd "$rundir" && timeout 600 "$SIM" "$ENGINE" -r "$xraw" -a "$xdeck" ) >> "$LOG" 2>&1 \
      || die "$ENGINE run"
    [ -s "$xraw" ] || die "no $ENGINE rawfile"
    ;;
  ngspice|ngspice@*)
    # qspice2xyce does the dialect lift (trailing NMOS tokens, BJT substrate,
    # u->µ, .lib path remap) that ngspice also needs; the Xyce-only cards are
    # harmless or ignored for standard decks.
    perl "$Q2X" -o "$xdeck" "$deck" >> "$LOG" 2>&1 || die "qspice2xyce (ngspice)"
    ngbody="$xdeck"; preosdi=""
    # Verilog-A path: OpenVAF-compile .hdl modules to OSDI and rewrite the Xyce
    # Y<MODULE> devices to ngspice N<inst>...<module> (reuses bfit's OSDI pipeline).
    if grep -qiE '^[[:space:]]*\.(hdl|va)[[:space:]]|^[[:space:]]*Y[A-Za-z0-9_]' "$xdeck"; then
      wk="$base.qshim.ngwork"; rm -rf "$wk"; mkdir -p "$wk"
      perl "$QSHIM_DIR/va2ngspice.pl" "$xdeck" "$wk" "$OPENVAF" "$rundir" "$VA_PATH" >> "$LOG" 2>&1 \
        || die "va2ngspice (Verilog-A -> OSDI)"
      ngbody="$wk/body.cir"; preosdi="$wk/pre_osdi.txt"
    fi
    # wrap with an ngspice control block (pre_osdi loads, then ASCII rawfile out)
    ngdeck="$base.qshim.ng.cir"
    sed '/^\.end$/Id' "$ngbody" > "$ngdeck"
    { echo ".control"; [ -s "$preosdi" ] && cat "$preosdi"
      printf 'set filetype=ascii\nrun\nwrite %s\nquit\n.endc\n.end\n' "$xraw"; } >> "$ngdeck"
    ( cd "$rundir" && timeout 600 "$SIM" "$ENGINE" -b "$ngdeck" ) >> "$LOG" 2>&1 || die "ngspice run"
    [ -s "$xraw" ] || die "no ngspice rawfile"
    ;;
  *) die "unknown engine: $ENGINE" ;;
esac
perl "$QSHIM_DIR/raw2qraw.pl" "$xraw" "$deck" "${op_orig:-$deck}" > "$qraw" || die "raw2qraw"
[ -s "$qraw" ] || die "empty qraw"
[ -n "$out" ] && { case "$out" in [A-Za-z]:*|*\\*) out=$(wslpath -u "$out");; esac
                   echo "Simulated by Xyce via qspice-shim" > "$out"; }
log "OK -> $qraw"
exit 0
