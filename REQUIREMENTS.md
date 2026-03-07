# Interactive Xyce Control — Requirements

## Three consumers, one API

All three consumers need the same core capability: load a circuit, step the
simulation forward, read back node voltages/currents, and modify parameters
at runtime. The differences are in transport and lifecycle.

## Core Simulation Control API

These are the primitives everything else is built on.

| # | Capability | Xyce C API today | Gap |
|---|-----------|-----------------|-----|
| 1 | **Load circuit** from netlist string/file | `xyce_initialize(argv)` | Works, but only once per session — no reload/`source` |
| 2 | **Run to completion** | `xyce_runSimulation()` | Works |
| 3 | **Step to time T** | `xyce_simulateUntil(t)` | Works — returns actual time reached |
| 4 | **Pause/halt** a running sim | — | **Missing** — no async cancel; `simulateUntil` is synchronous |
| 5 | **Query node voltage** at current time | `xyce_obtainResponse("V(node)")` | Works for named nodes; need to enumerate available names |
| 6 | **Query branch current** | `xyce_obtainResponse("I(device)")` | Works if `.PRINT` references it; enumeration missing |
| 7 | **List all node/vector names** | — | **Missing** — no equivalent of ngspice `AllVecs`/`AllPlots` |
| 8 | **Get full waveform data** (time + values array) | — | **Missing** — `obtainResponse` returns scalar at current time only |
| 9 | **Modify parameter at runtime** | `xyce_setCircuitParameter("R1:R", val)` | Works for device instance params |
| 10 | **Check simulation complete** | `xyce_simulationComplete()` | Works |
| 11 | **Get current/final time** | `xyce_getTime()` / `xyce_getFinalTime()` | Works |
| 12 | **Reset and re-run** same or different circuit | — | **Missing** — must close/reopen |
| 13 | **Background (async) run** | — | **Missing** — ngspice has `bg_run`/`bg_halt` threading |
| 14 | **Raw file output** | `-r file` CLI flag | Works, but only at end of full run |

## Consumer-Specific Requirements

### A. Standalone CLI (interactive REPL)

An ngspice-like interactive shell for Xyce.

| # | Command | Maps to |
|---|---------|---------|
| A1 | `source <file>` | Load/reload a netlist |
| A2 | `run` | Run simulation to completion |
| A3 | `step <time>` | `simulateUntil(t)` — advance by delta |
| A4 | `stop` / Ctrl-C | Halt a running simulation |
| A5 | `print V(out)` | `obtainResponse` — show value at current time |
| A6 | `show` | List all nodes and their current values |
| A7 | `alter R1 1k` | `setCircuitParameter` — change device param |
| A8 | `status` | Show sim time, progress, completion state |
| A9 | `reset` | Restart sim from t=0 (requires close/reopen) |
| A10 | `write <file.raw>` | Dump accumulated waveform to .raw file |
| A11 | `quit` | Clean shutdown |
| A12 | `devices` | List all devices in the circuit |
| A13 | `param <dev:param>` | Query a device parameter value |

### B. KiCad Integration (ngspice API shim)

Drop-in `libngspice.so` replacement. KiCad calls these functions:

| # | ngspice function | Implementation via Xyce |
|---|-----------------|----------------------|
| B1 | `ngSpice_Init(callbacks...)` | Store callback pointers; create Xyce instance |
| B2 | `ngSpice_Circ(char**)` | Write lines to temp file → `xyce_initialize` |
| B3 | `ngSpice_Command("run")` | `xyce_runSimulation` in background thread |
| B4 | `ngSpice_Command("bg_run")` | Same, threaded; call `BGThreadRunning` callback |
| B5 | `ngSpice_Command("bg_halt")` | Set flag to stop `simulateUntil` loop |
| B6 | `ngSpice_Command("reset")` | Close + reopen Xyce instance |
| B7 | `ngSpice_running()` | Return thread-running flag |
| B8 | `ngGet_Vec_Info(name)` | Return stored waveform data as `vector_info` |
| B9 | `ngSpice_CurPlot()` | Return plot name string ("tran1", "ac1", etc.) |
| B10 | `ngSpice_AllPlots()` | Return array of plot names |
| B11 | `ngSpice_AllVecs(plot)` | Return array of vector names for plot |
| B12 | Callbacks: `SendChar` | Forward Xyce stdout/stderr |
| B13 | Callbacks: `SendData` | Accumulate data during `simulateUntil` loop, call back |
| B14 | Callbacks: `SendInitData` | After init, report vector info |
| B15 | Callbacks: `SendStat` | Progress percentage during sim |

**Key challenge**: Xyce has no vector enumeration or waveform storage API.
The shim must accumulate all data points internally during simulation and
serve them back through `ngGet_Vec_Info`.

### C. NVC Mixed-Signal Co-Simulation

VHDL simulator (NVC) drives the clock; Xyce simulates analog subcircuits.

| # | Capability | Implementation |
|---|-----------|---------------|
| C1 | **Lock-step advancing** | NVC calls `simulateUntil(t)` at each VHDL delta |
| C2 | **Inject digital→analog** | `updateTimeVoltagePairs` on DAC devices |
| C3 | **Sample analog→digital** | `obtainResponse("V(node)")` or ADC interface |
| C4 | **Multiple instances** | Separate Xyce objects per analog block |
| C5 | **Shared event queue** | NVC notifies Xyce of next event time |
| C6 | **Parameter handoff** | Pass VHDL generics → Xyce `.PARAM` |
| C7 | **Bidirectional sync** | Xyce must report when it needs a shorter step (convergence) |

**Xyce already has DAC/ADC device support** (`YDAC`, `YADC`) designed for
exactly this use case. The C API's `updateTimeVoltagePairs` and
`getTimeVoltagePairsADC` are the interface. The gap is orchestration —
who drives the time loop and how events synchronize.

## Gaps Summary (what must be built)

| Gap | Priority | Difficulty | Needed by |
|-----|----------|-----------|-----------|
| **Vector enumeration** (list all nodes/vectors) | High | Medium | CLI, KiCad |
| **Waveform storage** (full time-series arrays) | High | Medium | CLI, KiCad |
| **Circuit reload** without process restart | Medium | Hard | CLI, KiCad |
| **Async/background run** with halt | Medium | Medium | KiCad |
| **Progress callbacks** | Low | Easy | KiCad |
| **NVC event sync protocol** | High | Hard | NVC |

## Implementation Plan

### Phase 1: Python CLI (this sprint)
- Use existing `xyce_interface.py` via ctypes to `libxycecinterface.so`
- Build interactive REPL with readline
- Implement: source, run, step, print, show, alter, status, devices, quit
- Accumulate waveform data in Python during `simulateUntil` loop
- `write` command dumps to `.raw` format
- **Prerequisite**: Build Xyce with `BUILD_SHARED_LIBS=ON`

### Phase 2: ngspice API shim (C shared library)
- `libngspice.so` drop-in that wraps Xyce
- Implement the 11 ngspice functions KiCad calls
- Thread management for bg_run/bg_halt
- Vector storage + enumeration layer

### Phase 3: NVC bridge
- Define VHPI/VPI foreign function interface
- Lock-step co-simulation driver
- DAC/ADC device mapping from VHDL ports
