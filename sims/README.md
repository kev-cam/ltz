# sims — the /opt/sims engine registry + fresh-box bootstrap

A single selector funnels QSPICE GUI decks (via [`../qshim`](../qshim)) to any
installed simulator. The engines live, self-contained, under `/opt/sims`; this
directory installs and bootstraps them.

## Three layers

```
bootstrap.ps1      (Windows, elevated once)  install WSL + the right Ubuntu
   └─ bootstrap-wsl.sh  (WSL, as root)        apt deps + work user + symlink
        └─ install-sims.sh (WSL, work user)   copy engines into /opt/sims
             └─ /opt/sims/sim <engine> ...     run any engine; `sim --list`
```

### `bootstrap.ps1` — fresh Windows box → working funnel
```
powershell -ExecutionPolicy Bypass -File bootstrap.ps1
   [-Distro Ubuntu] [-User claude] [-WithMingw] [-DeployShim]
   [-XyceArtifacts D:\sims-xyce.tar.gz]
```
Enables the WSL platform (reboots once if needed), installs/reuses an `Ubuntu*`
distro, and provisions it **as root** (so no interactive distro OOBE). The
registered distro is usually just named `Ubuntu` (it tracks the latest Ubuntu —
26.04 here); the script reuses any existing `Ubuntu*`. `-DeployShim` also runs
`../qshim/install.ps1` to swap the QSPICE engine for the shim.

### `bootstrap-wsl.sh` — apt deps + provisioning (run as root)
Installs the exact dependency set (mapped from the engines' `ldd` closure on
26.04): `libblas3 liblapack3 libgfortran5 libfftw3-double3 libamd3
libsuitesparseconfig7 libyaml-cpp0.8 libgomp1` (Xyce serial) · `openmpi-bin`
(pulls the whole MPI transport stack for xyce-mpi) · `ngspice` (pulls X11/readline
deps **and** is the prebuilt binary+codemodels source install-sims bundles) ·
`perl make`. Creates the work user, sets `/usr/local/src → /mnt/c/.../src`, and
runs `install-sims.sh`.

## Manager (`sims`)
A single tool at `/opt/sims/sims` for day-to-day use and self-maintenance:
```
sims list                      engines / versions / current
sims run <engine[@ver]> ...     run an engine (== /opt/sims/sim; bridge.sh uses this)
sims doctor [engine]            health-check: binary, ldd resolves, ngspice codemodels load,
                                openvaf runs, pdk present, ssh perms  -> PASS/FAIL
sims repair                     doctor, then fix ONLY what's broken (re-restore the missing
                                artifact / re-bundle ngspice / re-apt / re-link)
sims extras [list|<name>...]    glayout | devtools | kestrel
sims add|remove <engine[@ver]>  register / drop a version
sims update [--dry-run]         regression-gated promote/rollback (see below)
sims sync                       reconcile the install to the manifest
sims pack [dir]                 capture artifacts (pack-artifacts.sh)
```
**Manifest** `/opt/sims/sims.conf` declares the desired environment (`engine <name>
<ver>` / `extra <name>`); fork it per box, and `sims sync` makes the install match —
touching only the diffs.

**Extras.** `glayout` (restore `/opt/glayout-env`), `devtools` (git/gtkwave/…),
`kestrel` — [kev-cam/kestrel](https://github.com/kev-cam/kestrel), a Python PLL /
circuit generator installed into a venv at `/opt/kestrel` (clone → `python3-venv`
→ `pip install -e`). It emits Verilog-AMS + SPICE + KiCad + an ngspice/OSDI
testbench; the continuous blocks (charge pump, loop filter) run on the funnel via
the OpenVAF/OSDI ngspice path, the event-driven blocks (PFD/divider/VCO) hit the
OpenVAF "no event VA" limit (see qshim README) and use the SPICE testbench.

**Agentic updates** `sims update` (executor: `sims-autoupdate.sh`) reads a catalog
`/opt/sims/catalog.conf` (`<engine> <version> <source>`; source = tarball/dir). For each
entry newer than `current`: stage the candidate under `…/<engine>/<newver>`, run a
regression vs the running version (default: a smoke deck, key output within `SIMS_TOL`,
2 %), then **promote** (flip `current`) only if green, else **roll back** (drop the
candidate). Nothing is promoted unproven. Drive it from a scheduler or a Claude routine;
the agent's job is to maintain the catalog and triage rollbacks (read `autoupdate.log`).

### `install-sims.sh` — populate /opt/sims (idempotent, reversible)
`/opt/sims/<engine>/<version>/{bin,lib,run}` + a `current` symlink, each engine
self-contained (its `run` pins `LD_LIBRARY_PATH` to its own `lib/`). ngspice
also bundles its XSPICE codemodels + `spinit` (`SPICE_LIB_DIR` points at them).
`rm -rf /opt/sims` fully reverses it. Sources are overridable via env
(`XY_SER_BIN`, `XY_MPI_BIN`, `NG_BIN`, …).

## Making WSL disposable: the artifact set
apt packages + the Cygwin `/usr/local/src` repo tree are reproducible. Everything
else of value lives only in WSL and a `wsl --unregister` would vaporize it — so
`pack-artifacts.sh` captures it on the golden box and `bootstrap-wsl.sh` restores
it. Together: **bootstrap + artifacts + the Cygwin tree == the whole environment.**

| artifact | holds | why not just refetch |
|---|---|---|
| `sims-xyce.tar.gz` | `/opt/sims/{xyce,xyce-mpi}` (self-contained engines) | **Xyce has no apt package** |
| `openvaf.tar.gz` | `/opt/openvaf` (OpenVAF 23.5.0) | a **custom local build**, no public download matches |
| `pdk-sky130.tar.gz` | `/opt/pdk` (sky130A+B) | PDK install has py3.14/no-pip/zstd gotchas |
| `wsl-ssh-backup.tar.gz` | `~/.ssh` + `.gitconfig` | **private key is unrecoverable** |
| `xyce-build-mpi.tar.gz` | `~/xyce-build-mpi` + `~/trilinos-mpi` (1.5 G) | rebuild/relink capability — **opt-in** (`WITH_BUILD_TREES=1`) |

```
# golden box, once (default out: C:\cygwin64\home\Claude\sims-artifacts):
bash pack-artifacts.sh                      # WITH_BUILD_TREES=1 to also pack the MPI build tree
# fresh box (default -ArtifactsDir is that same path):
bootstrap.ps1                               # restores all present artifacts (FORCE_RESTORE=1 to overwrite)
```
ngspice is *not* an artifact — it comes from apt and `install-sims` bundles it.
Restore skips any target that already exists (idempotent); a missing artifact is
skipped with a NOTE, not an error.
