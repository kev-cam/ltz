# ltz

**The last proprietary simulator you'll ever need to uninstall.**

ltz unifies [KiCad](https://www.kicad.org/) schematic capture with [Xyce](https://xyce.sandia.gov/) parallel SPICE simulation into a seamless, open-source analog design workflow. Draw your circuit. Press Run. See your waveforms.

## Why

LTspice and Qspice are closed-source, single-threaded, vendor-locked dead ends. ltz is the open-source platform that replaces them — and extends into mixed-signal co-simulation, chiplet interposer design, and federated multi-die verification where they can never follow.

## Status

Early development. See [MISSION.md](MISSION.md) for the full vision.

## Running on Windows

The SPICE engines run under WSL, and a QSPICE-GUI shim can funnel schematics to
**Xyce**, **Xyce-MPI**, or **ngspice**. See
**[docs/INSTALL-Windows.md](docs/INSTALL-Windows.md)** for the end-to-end setup
(`sims/` bootstrap + engine registry, `qshim/` GUI shim).

## Architecture

```
KiCad Schematic Editor
        │
        ▼
  ltz Plugin (Python)
  ├── Netlist export (KiCad → Xyce)
  ├── Simulation control (invoke Xyce)
  └── Waveform display (results → KiCad sim UI)
        │
        ▼
  Xyce SPICE Engine
```

## Roadmap

- [ ] KiCad Action Plugin: one-click Xyce simulation from schematic
- [ ] LTspice .asc → Xyce netlist conversion pipeline
- [ ] Automated compatibility testing against 27k+ community circuits
- [ ] Curated component library with Xyce-validated SPICE models
- [ ] Waveform viewer integration
- [ ] Interposer design flow (KiCad PCB editor for chiplet substrates)
- [ ] Federated simulation bridge (Xyce + NVC + gHDL + Verilator)

## Quick Start

```bash
# Clone ltz
git clone https://github.com/kev-cam/ltz.git
cd ltz

# Fetch LTspice community test circuits into ../ltz-tests
./scripts/fetch_tests.sh

# Scan for Xyce compatibility
python3 tools/ltz_convert.py --scan ../ltz-tests/ecircuit/

# Batch convert self-contained circuits
python3 tools/ltz_convert.py --batch --self-contained ../ltz-tests/ecircuit/ -o tests/converted/

# Run a circuit through Xyce
Xyce tests/converted/00_RC_LOW_PASS_FILTER/Lpfilter1.cir
```

## Dependencies

- [Xyce](https://xyce.sandia.gov/) 7.8+ (built from source or package)
- Python 3.10+
- [spicelib](https://github.com/nunobrum/spicelib) (LTspice .asc parsing — `pip install spicelib`)
- [KiCad](https://www.kicad.org/) 9.0+ (later phases only — not needed yet)

## Repository Layout

```
ltz/
├── MISSION.md              # Project vision
├── README.md
├── REQUIREMENTS.md         # Interactive Xyce control — requirements
├── bin/                    # Drop-in CLI front-ends that run circuits through Xyce
│   ├── LTspice             #   LTspice-compatible front-end → Xyce
│   ├── ltz                 #   ltz driver
│   └── ltz-kicad           #   KiCad-integrated driver
├── docs/
│   ├── INSTALL-Windows.md  # Windows / WSL setup walkthrough
│   └── LTSPICE_XYCE_HANDOFF.md
├── examples/               # Runnable demos: RC filter, PLL, KiCad projects
├── lib/                    # ngspice_shim/ (KiCad↔ngspice) + standard.lib models
├── ltwatch/                # View Xyce output in the LTspice waveform viewer
│   ├── ltwatch.sh
│   └── raw2ltraw.pl        #   Xyce .raw → LTspice .raw
├── pop/                    # ltz-pop: netlist parse/generate (see PLAN.md)
├── qshim/                  # QSPICE-GUI shim → funnel decks to Xyce / ngspice
│   ├── bridge.sh           #   WSL pipeline: translate → sim → .qraw
│   ├── va2ngspice.pl       #   Verilog-A → OSDI (OpenVAF) for ngspice
│   └── install.ps1         #   swap the QSPICE engine exe for the shim
├── scripts/
│   └── fetch_tests.sh      # Populates ../ltz-tests with community circuits
├── sims/                   # WSL SPICE-engine funnel: /opt/sims registry + bootstrap
│   ├── bootstrap.ps1       #   fresh Windows → WSL Ubuntu + engines
│   ├── install-sims.sh     #   build the engine registry
│   ├── pack-artifacts.sh   #   capture non-apt state (makes WSL disposable)
│   ├── sims                #   manager: list/run/doctor/repair/extras/update/sync
│   └── sims-autoupdate.sh  #   regression-gated engine promote/rollback
├── tools/
│   └── ltz_convert.py      # LTspice → Xyce netlist converter
└── tests/
    └── converted/          # Xyce-ready netlists (generated)

../ltz-tests/                # Sibling dir (not in repo, created by fetch_tests.sh)
├── circuits-ltspice/        # mick001 educational circuits
├── ecircuit/                # eCircuit Center .cir netlists
├── powersim/                # Power electronics simulations
├── spice-libraries/         # Community SPICE models
└── ...
```

## License

TBD

## Contributing

Not yet accepting external contributions. Watch this space.

---

*A [Cameron EDA](https://cameroneda.com) project.*
