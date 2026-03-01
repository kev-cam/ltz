# Project Mission Statement

## The Last Proprietary Simulator You'll Ever Need to Uninstall

Analog circuit simulation has been held hostage by vendor lock-in for decades. LTspice ties you to Analog Devices. Qspice ties you to Qorvo. Both are closed-source, single-threaded, analog-only dead ends that cannot follow modern electronics into the multi-die, mixed-signal, chiplet era.

We are building the open-source platform that replaces them.

### What We're Doing

Unifying **KiCad** — the world's leading open-source schematic capture and PCB design suite — with **Xyce** — Sandia National Laboratories' massively parallel SPICE engine — into a seamless, zero-friction simulation workflow. Draw your circuit. Press Run. See your waveforms. No export steps. No format conversions. No vendor permission.

### Why This Wins

- **KiCad** brings 1nm internal resolution, Python-scriptable design automation, 32 copper layers, and a global community of contributors. It already speaks PCB. We're teaching it to speak interposer.

- **Xyce** brings distributed-memory parallel simulation, superior convergence through homotopy and continuation methods, native Verilog-A support, and the ability to scale from a laptop to a cluster. It solves circuits that make LTspice fail silently.

- **Together** they create something neither LTspice nor Qspice can ever become: an open platform where analog simulation is the entry point to a federated verification environment spanning SPICE, digital RTL, mixed-signal co-simulation, and multi-die system integration.

### What We Ship

1. **A KiCad plugin** that makes Xyce simulation as simple as LTspice: one click from schematic to waveform.

2. **An LTspice compatibility layer** validated against 27,000+ community circuits, with automated regression proving what runs and what runs better.

3. **A curated component library** linking KiCad symbols to Xyce-validated SPICE models for the parts engineers actually use.

4. **An interposer design flow** that repurposes KiCad's PCB editor for chiplet substrate layout — because the future of electronics is heterogeneous integration, and the tools should be ready.

5. **A federated simulation bridge** connecting Xyce to open-source digital simulators (NVC, gHDL, Verilator) for mixed-signal and multi-die verification that no closed-source tool offers at any price.

### Who This Is For

Every engineer who has hit an LTspice convergence wall at 2 AM. Every team that needs batch simulation in CI/CD and can't script a GUI. Every startup that refuses to build on a platform whose source code they'll never see. Every researcher who needs parallel SPICE and shouldn't have to pay six figures for it. Every designer staring down a chiplet interposer and finding zero affordable tools for the job.

### The Standard We Hold

If an LTspice user can't switch to this platform in under ten minutes and run their existing circuits, we haven't finished. Compatibility is the floor. Capability is the ceiling. Open source is the foundation.

---

*Cameron EDA — Open tools for the engineers who build what's next.*
