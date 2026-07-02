# qshim — funnel the QSPICE GUI to any installed simulator

Swap QSPICE's engine exes for a shim so schematics drawn (and waveforms
viewed) in the QSPICE GUI simulate on **whichever engine you select** —
real QSPICE, **Xyce**, **Xyce-MPI**, or **ngspice** — chosen by a one-token
`mode.txt`, resolved against the `/opt/sims` engine registry in WSL.

```
QUX.exe (GUI) ──spawns──> QSPICE64.exe            <- our shim (PE, mingw-built)
        mode.txt ──────────┤
                            ├ passthru:  QSPICE64.real.exe (untouched)
                            └ <engine>:  wsl.exe bridge.sh --pid N  (engine= in args file)
                                           ├ qspice2xyce.pl          (dialect lift)
                                           ├ sim <engine>            (/opt/sims launcher)
                                           │    xyce | xyce-mpi | ngspice[@version]
                                           └ raw2qraw.pl             (-> .qraw for the viewer)
                                         (falls back to the real engine on any failure)
```

Engines live under `/opt/sims/<engine>/<version>/` (self-contained: each `run`
launcher pins its own `LD_LIBRARY_PATH`, so the serial and MPI Trilinos sets —
identical SONAMEs, different ABIs — never collide). Install/refresh them with
`ltz/sims/install-sims.sh`; list with `/opt/sims/sim --list`; run one directly
with `sim <engine[@version]> <args>`.

## Install (elevated)
```
cd ltz\qshim
powershell -ExecutionPolicy Bypass -File install.ps1
```
Uninstall: same with `-Restore`. The real engines are kept beside the shim as
`*.real.exe`; the GUI keeps working in every mode.

## Mode & logs (per-user, no elevation)
- `%LOCALAPPDATA%\qspice-shim\mode.txt` — the engine to funnel to: `passthru`
  (default, real engine), or any `/opt/sims` engine token: `xyce`, `xyce-mpi`,
  `ngspice`, or pinned `engine@version`. Switching engines is just editing this
  file — no reinstall.
- `%LOCALAPPDATA%\qspice-shim\shim.log` — every engine invocation (protocol discovery)
- `ltz/qshim/bridge.log` — WSL-side pipeline log

After changing the shim binary (not the mode), redeploy it elevated with
`install.ps1 -Update` (refreshes the shim without touching the `.real.exe`
swap). The older single-mode shim is forward-compatible: with no `engine=` in
the args file the bridge defaults to `xyce`.

## ngspice bridge
ngspice reuses the `qspice2xyce.pl` dialect lift (trailing NMOS tokens, BJT
substrate node, `.lib` path remap all apply to ngspice too), then runs under an
`.control … set filetype=ascii / run / write / .endc` block that emits a SPICE3
ASCII rawfile; `raw2qraw.pl` normalizes ngspice's pre-wrapped `v(node)`/`i(src)`
names to QSPICE `V(node)`/`I(SRC)`.

### Verilog-A on ngspice (OpenVAF → OSDI)
When the translated deck has `.hdl` includes or `Y<MODULE>` devices, the bridge
runs `va2ngspice.pl` (reusing bfit's `drivers_ngspice` OSDI pipeline): each `.va`
is OpenVAF-compiled to `.osdi` (loaded via `pre_osdi` in the `.control` block),
and each `Y<MODULE> <inst> <nodes> <params>` becomes a per-instance
`.model <mod>_<inst> <mod>(<model params>)` + `N<inst> <nodes> <mod>_<inst>
<instance params>` — params are split by reading `(* type="instance" *)` in the
`.va`. Xyce-only module attributes (`xyceModelGroup`/`xyceLevelNumber`) are
stripped before OpenVAF. Verified end to end: a clean VA cap gives the same RC
response (0.732) as the native cap.

**OpenVAF caveats** (model-side, not the bridge): OpenVAF is a compact-model
compiler, so it rejects event-driven VA (`@(cross …)`, timers — e.g. the QSPICE
`phasedet`/`divn`), and the QSPICE-VAE `qspice_*` models use a node-collapse
idiom (`V(p,mid)<+RSER*I(…)`) that OSDI doesn't honour (params bind but the
device is inert). Standard OpenVAF-idiom models work; on any failure the bridge
exits nonzero and the shim falls back to the real engine.

## Build
```
x86_64-w64-mingw32-gcc -O2 -s -o qspice-shim.exe qspice-shim.c   # in WSL
```

## Status
- Validated in sandbox: file-based invocation (`deck.cir -ascii -r out.qraw`)
  end-to-end, 4s wall. The qraw carries QSPICE spellings (V(x), I(src)).
- QUX's marching-waveform mode pipes the netlist on **stdin** and may expect a
  **streamed binary** qraw; the bridge captures stdin but emits a final ASCII
  qraw — first GUI runs should use passthru mode to harvest the exact protocol
  from shim.log, then graduate to xyce mode.
- The `-viewer`/`-simProcessID` handshake args are accepted and ignored.
- Requires: WSL Xyce engine (see ltz docs), xyce repo checkout for
  qspice2xyce.pl, this dir reachable at
  `/mnt/c/cygwin64/usr/local/src/ltz/qshim` (path baked into the shim;
  rebuild if relocated).

## Working (2026-06-11)
GUI-on-Xyce confirmed end to end: Run in QUX -> shim -> WSL bridge -> Xyce ->
binary .qraw with Plot Suggestion(s) -> waveform displayed (AudioAmp, ~5s).
The protocol pieces that mattered: netlist arrives on stdin (-pipe); args
cross to WSL via an args-file (backslashes/quotes don't survive wsl.exe argv
marshaling); the .qraw must be BINARY with a byte-faithful header; QUX
auto-plots only what "Plot Suggestion(s):" names; 0x80FF announces the
engine's own window (HWND), 0x8100 streams console text 8 chars/post.
