#!/usr/bin/env bash
#
# sims-autoupdate.sh -- incremental engine updates with regression gating.
# The deterministic executor under the "agentic manager": a scheduler (cron /
# Claude routine) runs this; an agent's job is to maintain the catalog and triage
# any rollback (read $LOG, decide whether to chase the regression or pin a version).
#
# Catalog (one line per available build):  <engine> <version> <source>
#   source = a .tar.gz of the engine tree, OR a directory to copy, OR `build:<recipe>`
# For each catalog entry NEWER than the engine's current version:
#   stage candidate -> regress vs current -> promote (flip `current`) if green,
#   else roll back (drop the candidate, keep current). Nothing is promoted unproven.
#
#   sims-autoupdate.sh [--dry-run] [--engine NAME]
set -o pipefail
export PATH=/usr/sbin:/usr/bin:/sbin:/bin:/usr/local/sbin:/usr/local/bin

ROOT=/opt/sims
CATALOG="${SIMS_CATALOG:-$ROOT/catalog.conf}"
TOL="${SIMS_TOL:-0.02}"                 # 2% inter-version agreement (no sim is gospel)
LOG="${SIMS_LOG:-$ROOT/autoupdate.log}"
DECK="${SIMS_REGRESS_DECK:-/mnt/c/cygwin64/tmp/claude/rc.cir}"   # default smoke deck
Q2X=/usr/local/src/xyce/utils/qspice2xyce.pl
DRY=0; ONLY=""
while [ $# -gt 0 ]; do case "$1" in --dry-run) DRY=1;; --engine) ONLY="$2"; shift;; esac; shift; done

log(){ echo "[$(date '+%F %T' 2>/dev/null || echo now)] $*" | tee -a "$LOG"; }
curver(){ readlink "$ROOT/$1/current" 2>/dev/null; }

# --- regression: run DECK on candidate + current, compare a key output -------
# Returns 0 (green, within TOL) or 1 (regressed). Swap in model_bench.sh / the
# ltz regress harness here for full coverage; this is the gating smoke test.
run_metric(){  # engine version -> prints V(out) peak (or empty)
  local eng="$1" ver="$2" run="$ROOT/$eng/$ver/run" d; d=$(mktemp -d)
  local xdeck="$d/in.cir" raw="$d/out.raw"
  perl "$Q2X" -o "$xdeck" "$DECK" >/dev/null 2>&1 || cp "$DECK" "$xdeck"
  case "$eng" in
    ngspice) sed '/^\.end$/Id' "$xdeck" > "$d/ng.cir"
             printf '.control\nset filetype=ascii\nrun\nwrite %s\nquit\n.endc\n.end\n' "$raw" >> "$d/ng.cir"
             "$run" -b "$d/ng.cir" >/dev/null 2>&1 ;;
    *)       ( cd "$d" && "$run" -r "$raw" -a "$xdeck" >/dev/null 2>&1 ) ;;
  esac
  python3 - "$raw" <<'PY' 2>/dev/null
import sys,re
try: t=open(sys.argv[1],errors='ignore').read()
except: sys.exit(0)
names=re.findall(r'^\s*\d+\s+(\S+)',t[t.find('Variables:'):t.find('Values:')],re.M)
vals=re.findall(r'-?\d+\.?\d*(?:[eE][-+]?\d+)?',t[t.find('Values:')+7:])
nv=len(names)
if not nv: sys.exit(0)
oi=next((i for i,n in enumerate(names) if 'out' in n.lower()),None)
if oi is None: sys.exit(0)
col=[float(vals[r*(nv+1)+1+oi]) for r in range((len(vals))//(nv+1))]
if col: print((max(col)-min(col))/2)
PY
  rm -rf "$d"
}
regress(){  # engine candver
  local eng="$1" cand="$2" cur; cur=$(curver "$eng")
  local a b; a=$(run_metric "$eng" "$cur"); b=$(run_metric "$eng" "$cand")
  [ -n "$a" ] && [ -n "$b" ] || { log "  regress($eng $cand): no metric (cur=$a cand=$b) -> FAIL"; return 1; }
  local rel; rel=$(python3 -c "print(abs($b-$a)/$a if $a else 9)")
  awk -v r="$rel" -v t="$TOL" 'BEGIN{exit !(r<=t)}' \
    && { log "  regress($eng $cand): cur=$a cand=$b rel=$rel <= $TOL  GREEN"; return 0; } \
    || { log "  regress($eng $cand): cur=$a cand=$b rel=$rel >  $TOL  REGRESSED"; return 1; }
}

stage(){  # engine version source
  local eng="$1" ver="$2" src="$3" dst="$ROOT/$eng/$ver"
  [ -d "$dst" ] && { log "  $eng@$ver already staged"; return 0; }
  log "  staging $eng@$ver from $src"
  case "$src" in
    *.tar.gz|*.tgz) mkdir -p "$dst.tmp"; tar -xzf "$src" -C "$dst.tmp"
                    # tarball may wrap in <engine>/<ver>/ or be the ver dir itself
                    if [ -d "$dst.tmp/$eng/$ver" ]; then mv "$dst.tmp/$eng/$ver" "$dst"; else mv "$dst.tmp" "$dst"; fi
                    rm -rf "$dst.tmp" ;;
    build:*)        log "  build recipe '${src#build:}' not wired -- supply a prebuilt tarball/dir"; return 1 ;;
    *)              [ -d "$src" ] || { log "  source missing: $src"; return 1; }; cp -a "$src" "$dst" ;;
  esac
  [ -x "$dst/run" ]
}

main(){
  : > /dev/null; touch "$LOG" 2>/dev/null
  [ -f "$CATALOG" ] || { log "no catalog $CATALOG (lines: <engine> <version> <source>)"; exit 0; }
  log "autoupdate start (tol=$TOL dry=$DRY)"
  while read -r eng ver src; do
    case "$eng" in ''|'#'*) continue;; esac
    [ -n "$ONLY" ] && [ "$eng" != "$ONLY" ] && continue
    local cur; cur=$(curver "$eng")
    [ "$ver" = "$cur" ] && { log "$eng: current already $ver"; continue; }
    # only move forward (string compare; versions are date/semver-ish)
    [ "$(printf '%s\n%s\n' "$cur" "$ver" | sort -V | tail -1)" = "$ver" ] || { log "$eng: $ver not newer than $cur, skip"; continue; }
    log "$eng: candidate $ver (current $cur)"
    [ "$DRY" = 1 ] && { log "  --dry-run: would stage+regress+promote"; continue; }
    stage "$eng" "$ver" "$src" || { log "  stage failed, skip"; continue; }
    if regress "$eng" "$ver"; then
      ln -sfn "$ver" "$ROOT/$eng/current"; log "  PROMOTED $eng -> $ver"
    else
      rm -rf "$ROOT/$eng/$ver"; log "  ROLLED BACK $eng@$ver (current stays $cur)"
    fi
  done < "$CATALOG"
  log "autoupdate done"
}
main
