# ltz

**The last proprietary simulator you'll ever need to uninstall.**

ltz unifies [KiCad](https://www.kicad.org/) schematic capture with [Xyce](https://xyce.sandia.gov/) parallel SPICE simulation into a seamless, open-source analog design workflow. Draw your circuit. Press Run. See your waveforms.

## Why

LTspice and Qspice are closed-source, single-threaded, vendor-locked dead ends. ltz is the open-source platform that replaces them — and extends into mixed-signal co-simulation, chiplet interposer design, and federated multi-die verification where they can never follow.

## Status

Early development. See [MISSION.md](MISSION.md) for the full vision.

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

## Dependencies

- [KiCad](https://www.kicad.org/) 9.0+
- [Xyce](https://xyce.sandia.gov/) 7.8+
- Python 3.10+
- [spicelib](https://github.com/nunobrum/spicelib) (LTspice .asc parsing)

## License

TBD

## Contributing

Not yet accepting external contributions. Watch this space.

---

*A [Cameron EDA](https://cameroneda.com) project.*
