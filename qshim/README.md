# qshim — run the QSPICE GUI on Xyce

Swap QSPICE's engine exes for a shim so schematics drawn (and waveforms
viewed) in the QSPICE GUI simulate on **Xyce**.

```
QUX.exe (GUI) ──spawns──> QSPICE64.exe            <- our shim (PE, mingw-built)
                            ├── mode=passthru:  QSPICE64.real.exe (untouched)
                            └── mode=xyce:      wsl.exe bridge.sh
                                                  ├ qspice2xyce.pl   (translate)
                                                  ├ Xyce             (simulate)
                                                  └ raw2qraw.pl      (-> .qraw for the viewer)
                                                (falls back to the real engine on any failure)
```

## Install (elevated)
```
cd ltz\qshim
powershell -ExecutionPolicy Bypass -File install.ps1
```
Uninstall: same with `-Restore`. The real engines are kept beside the shim as
`*.real.exe`; the GUI keeps working in every mode.

## Mode & logs (per-user, no elevation)
- `%LOCALAPPDATA%\qspice-shim\mode.txt` — `passthru` (default) or `xyce`
- `%LOCALAPPDATA%\qspice-shim\shim.log` — every engine invocation (protocol discovery)
- `ltz/qshim/bridge.log` — WSL-side pipeline log

## Build
```
x86_64-w64-mingw32-gcc -O2 -s -o qspice-shim.exe qspice-shim.c   # in WSL
```

## Status / caveats
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
