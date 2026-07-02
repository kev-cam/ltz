# Installing the SPICE funnel on Windows

Run QSPICE schematics (and any translated deck) on **Xyce**, **Xyce-MPI**, or
**ngspice** from a Windows box. The engines run in **WSL**; a thin PowerShell
bootstrap sets everything up, and a shim lets the QSPICE GUI drive them.

```
Windows                                   WSL (Ubuntu)
────────                                  ────────────
bootstrap.ps1  ──►  wsl --install Ubuntu  ──►  apt deps + /opt/sims engines
QUX.exe (GUI)  ──►  qspice-shim  ──►  bridge.sh  ──►  sim <engine>  ──►  .qraw
```

## Prerequisites

- Windows 10 (2004+) or Windows 11, x64.
- An **elevated** PowerShell for the first run (installing the WSL platform /
  a distro needs admin, and may require **one reboot**). After that, day-to-day
  use needs no elevation.
- The `ltz` repo checked out locally (this guide assumes
  `C:\cygwin64\usr\local\src\ltz`; pass `-SrcRoot` to override).
- **Engine artifacts** — Xyce has no apt package, so the prebuilt engine trees
  are supplied as tarballs (see [Engine artifacts](#engine-artifacts)). ngspice
  is installed from apt automatically.
- *(Optional)* QSPICE installed at `C:\Program Files\QSPICE` — only needed to
  drive engines from the QSPICE GUI.

## Quick start

From an **elevated** PowerShell:

```powershell
cd C:\cygwin64\usr\local\src\ltz\sims
powershell -ExecutionPolicy Bypass -File bootstrap.ps1
```

That will:
1. Enable WSL and install Ubuntu if missing (reboot once if prompted, then
   re-run — it's idempotent).
2. Provision the distro as root: apt dependencies, a work user, and the
   `/usr/local/src` link back to this tree.
3. Restore engines + OpenVAF + the sky130 PDK + ssh keys from
   `-ArtifactsDir` (default `C:\cygwin64\home\Claude\sims-artifacts`), then
   populate `/opt/sims` and bundle ngspice.
4. Print `sims doctor` — every engine and component should read **PASS**.

Common options:

| Flag | Effect |
|------|--------|
| `-Distro Ubuntu-24.04` | pick a specific distro (default `Ubuntu`; reuses any existing `Ubuntu*`) |
| `-ArtifactsDir D:\sims-artifacts` | where the engine/PDK/ssh tarballs live |
| `-Extras glayout,devtools,kestrel` | also install optional components |
| `-DeployShim` | also swap the QSPICE GUI engine for the shim |
| `-WithMingw` | install mingw (only if rebuilding the shim) |
| `-Repair` | run `sims doctor` + fix only what's broken, then exit |

## Engine artifacts

Xyce is not in apt. On the **golden box** (one that already has the engines),
capture them once:

```powershell
wsl bash /usr/local/src/ltz/sims/pack-artifacts.sh          # add WITH_BUILD_TREES=1 for rebuild capability
```

This writes `sims-xyce.tar.gz`, `openvaf.tar.gz`, `pdk-sky130.tar.gz`,
`wsl-ssh-backup.tar.gz` (and optionally `glayout-env.tar.gz`,
`xyce-build-mpi.tar.gz`) to the artifacts dir. Copy that folder to the target
box and point `bootstrap.ps1 -ArtifactsDir` at it. Without artifacts, only
ngspice installs (the bootstrap prints a NOTE for the missing engines).

## Verify

```powershell
wsl /opt/sims/sim --list          # engines + versions
wsl /opt/sims/sims doctor         # PASS/FAIL per engine + component
```

Run a deck directly:

```powershell
wsl /opt/sims/sim xyce   -r out.raw -a mydeck.cir
wsl /opt/sims/sim ngspice -b mydeck.cir
NP=8 wsl /opt/sims/sim xyce-mpi -r out.raw -a mydeck.cir
```

## Driving the QSPICE GUI (optional)

```powershell
powershell -ExecutionPolicy Bypass -File ..\qshim\install.ps1        # first time (elevated)
powershell -ExecutionPolicy Bypass -File ..\qshim\install.ps1 -Update  # refresh the shim only
```

Then pick the engine — no elevation, no reinstall — by editing one file:

```
%LOCALAPPDATA%\qspice-shim\mode.txt   ->  passthru | xyce | xyce-mpi | ngspice
```

`passthru` runs the real QSPICE engine; any other value funnels the GUI's decks
to that `/opt/sims` engine, falling back to the real engine on any error so the
GUI never breaks. See [`qshim/README.md`](../qshim/README.md) for the protocol
and the Verilog-A / OSDI details.

## Maintaining the install

```powershell
wsl /opt/sims/sims doctor            # health check
wsl /opt/sims/sims repair            # fix only what's broken
wsl /opt/sims/sims extras kestrel    # add the PLL generator (etc.)
wsl /opt/sims/sims update            # regression-gated engine updates (catalog.conf)
wsl /opt/sims/sims sync              # reconcile the install to sims.conf
```

The WSL side is **disposable**: `bootstrap.ps1` + the artifact set + this repo
reconstruct the whole environment, so `wsl --unregister` + re-run is safe once
the artifacts are captured. Full reference: [`sims/README.md`](../sims/README.md).
