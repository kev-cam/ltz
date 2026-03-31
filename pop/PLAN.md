# ltz-pop Implementation Plan

Status of `ltz/pop/`:

| Module | Status |
|---|---|
| `netlist.py` | ✅ Done |
| `parse.py` | ✅ Done |
| `runner.py` | ⬜ Next |
| `cli.py` | ⬜ |
| `ic_file.py` | ⬜ |
| `ac.py` | ⬜ |
| `plot.py` | ⬜ |

---

## runner.py — Shooting Engine

The core POP loop.  Depends on `netlist.py` and `parse.py`.

**Class: `POPConfig` (dataclass)**

All tunable parameters in one place so `cli.py` can populate it directly
from argparse and pass it through without threading individual args:

```
trigger_node: str
period: float               # seconds
max_period_mult: float      # search bound (default 1.1)
pre_cycles: int             # warm-start transient cycles (default 5)
tol: float                  # convergence tolerance on state vector (default 1e-6)
max_iter: int               # (default 30)
solver: str                 # 'broyden' | 'newton' (default 'broyden')
xyce_bin: str               # (default 'Xyce')
work_dir: Path              # scratch dir (default .ltz-pop/)
verbose: bool
```

**Class: `POPResult` (dataclass)**

Returned by `POPRunner.run()`:

```
ic: dict[str, float]        # converged initial conditions, keyed by 'V(node)' / 'I(Lname)'
state_vars: list[StateVar]
x_ss: np.ndarray            # raw state vector, aligned with state_vars
iterations: int
history: list[tuple[int, float]]   # (iter, residual_norm) for convergence plot
converged: bool
```

**Class: `POPRunner`**

- `__init__(netlist, config)` — validate netlist exists, call `parse_state_vars`,
  mkdir `work_dir`.
- `_pre_cycle_run() -> np.ndarray` — run `pre_cycles * period` transient from
  zero ICs using `inject_ics` + `write_tran_directive` + `_xyce_run`.
  Returns final state as warm-start for shooter.
- `_shoot(x0) -> np.ndarray` — single Xyce run for one period, returns
  `x(T) - x0`.  Appends to `history`.  Named residual function for scipy.
- `run() -> POPResult` — orchestrates pre-cycle, then calls
  `broyden1` or `fsolve` on `_shoot`.

**`_xyce_run(netlist, bin)` (module-level helper)**

```python
result = subprocess.run([bin, str(netlist)], capture_output=True, text=True)
if result.returncode != 0:
    raise XyceError(result.stderr)
```

Raise a named `XyceError(Exception)` rather than bare `RuntimeError` so
`cli.py` can catch it and print a clean message without a traceback.

**Convergence notes**

- Broyden is the default: one Xyce run per iteration after the first,
  Jacobian built up incrementally.
- Newton (`fsolve`) uses finite-difference Jacobian: N+1 Xyce runs per step.
  Appropriate only when Broyden stalls (oscillatory circuits, very stiff loops).
- Both solvers get `f_tol=config.tol`.  Newton additionally gets `xtol`.
- After convergence, runner does one final Xyce run for exactly
  `5 * period` with the converged ICs to produce the steady-state
  waveform output the user actually looks at.

---

## ic_file.py — Converged IC Persistence

Read and write the `.ic` file format so POP results can be chained into
downstream Xyce runs or re-used as warm starts.

**File format** (valid Xyce netlist fragment, `.include`-able):

```spice
* ltz-pop converged initial conditions
* Source:    buck.cir
* Trigger:   SW
* Period:    2.000000us
* Solver:    broyden
* Iters:     12
* Residual:  3.41e-08
* Generated: 2026-04-01T14:32:11

.IC V(VOUT)=4.997341000
.IC V(VX)=0.000012300
.IC I(L1)=2.498821000
```

**Functions**

- `write_ic_file(path, result: POPResult, config: POPConfig, src: Path)` —
  writes the above.
- `read_ic_file(path) -> dict[str, float]` — parses `.IC` lines back to a
  dict, ignoring comment metadata.  Used as warm start for a second
  `ltz-pop` run on a modified netlist.
- `ic_to_array(ic_dict, state_vars) -> np.ndarray` — aligns a dict from
  `read_ic_file` with a `state_vars` list for injection.

---

## ac.py — AC Sweep and Bode Extraction

Run a small-signal AC analysis using the converged POP ICs.  This is
SIMPLIS's "AC without average models" capability.

**How it works**

1. Load converged ICs into a copy of the netlist via `inject_ics`.
2. Identify the AC injection point from a netlist annotation:
   `* AC_INJECT: FB` — the feedback node where the perturbation source
   is inserted.
3. Insert an AC voltage source in series at that node.
4. Replace `.TRAN` with `.AC DEC N fstart fstop`.
5. Run Xyce; parse the `.FD` output file (frequency-domain, produced by
   `.PRINT AC` with `FORMAT=PROBE`).
6. Return magnitude (dB) and phase (degrees) vs frequency.

**Netlist annotations**

```spice
* AC_INJECT: FB          required — feedback node
* AC_OUT: VOUT           optional — output node to probe (default: first C node)
```

**Functions**

- `setup_ac_netlist(src, dst, ic_dict, config: ACConfig)` — writes the
  modified netlist with ICs and AC source inserted.
- `parse_fd_file(path) -> ACResult` — reads `.FD` output.
- `run_ac(netlist, pop_result, config: ACConfig) -> ACResult` — end-to-end.

**Class: `ACConfig` (dataclass)**

```
fstart: float      # Hz (default 1.0)
fstop: float       # Hz (default 10e6)
points_per_dec: int  # (default 20)
inject_node: str | None   # override annotation
out_node: str | None
xyce_bin: str
work_dir: Path
```

**Class: `ACResult` (dataclass)**

```
freq: np.ndarray       # Hz
magnitude_db: np.ndarray
phase_deg: np.ndarray
gain_margin_db: float | None
phase_margin_deg: float | None
crossover_hz: float | None
```

Gain/phase margin and crossover frequency are computed automatically
from the Bode data — these are the numbers a power electronics engineer
actually needs from the AC analysis, not just the raw curves.

---

## plot.py — Waveform and Bode Display

Thin matplotlib wrapper.  Optional dependency — `ltz-pop` works without
it if matplotlib is not installed (the `--plot` flag errors gracefully).

**Functions**

- `plot_waveforms(time, matrix, state_vars, title)` — steady-state
  waveforms for all state variables on stacked subplots.  Marks one
  full switching period with a span.
- `plot_bode(ac_result, title)` — classic two-panel magnitude/phase Bode
  plot.  Annotates gain margin, phase margin, and crossover frequency
  with dashed lines.
- `plot_convergence(history)` — residual norm vs iteration number,
  log-scale y-axis.  Useful for diagnosing stalled convergence.

All three functions return a `matplotlib.figure.Figure` so the caller
can `savefig` or `show` as appropriate.

---

## cli.py — `ltz-pop` Entry Point

Thin argparse front-end.  No simulation logic here — just parse args,
build `POPConfig` / `ACConfig`, call `POPRunner`, write outputs.

**Argument groups**

```
positional:
  NETLIST               Input Xyce netlist

POP control:
  --trigger NODE
  --period FREQ_OR_T    e.g. 2us or 500kHz
  --max-period MULT     default 1.1
  --pre-cycles N        default 5
  --tol FLOAT           default 1e-6
  --max-iter N          default 30
  --solver {broyden,newton}

AC analysis:
  --ac                  Run AC sweep after POP
  --ac-start FREQ       default 1Hz
  --ac-stop FREQ        default 10MHz
  --ac-points N         default 20

Output:
  --out-ic FILE         Write converged ICs (default: <netlist>.ic)
  --no-ic               Suppress .ic file output
  --plot                Show waveform and Bode plots
  --work-dir DIR        default .ltz-pop/

Runtime:
  --xyce BIN            default Xyce
  -v, --verbose
```

**Exit codes**

```
0   Converged
1   Did not converge (max_iter reached)
2   Xyce error (nonzero return code)
3   Netlist error (missing trigger, unparseable)
```

**`pyproject.toml` entry point**

```toml
[project.scripts]
ltz-pop = "ltz.pop.cli:main"
```

---

## Build Order

Suggested implementation sequence — each step is independently testable:

1. **`runner.py`** — mock `_xyce_run` to return canned `.prn` files;
   verify Broyden converges on a linear test system in a few iterations.
2. **`ic_file.py`** — pure I/O, no Xyce dependency; test round-trip
   write/read.
3. **`cli.py`** — wire up args to runner; integration-test against a
   real Xyce run on the example buck netlist.
4. **`ac.py`** — requires a working POP result as input; test against
   a simple RC network with known Bode response before trying a full
   switching circuit.
5. **`plot.py`** — last, since it's pure display; can be developed
   interactively from a saved `POPResult`/`ACResult`.

---

## Test Fixtures Needed

- `tests/pop/fixtures/buck.cir` — minimal synchronous buck (L, C, ideal
  switches, voltage-mode PWM controller) with `POP_TRIGGER` and
  `POP_PERIOD` annotations.
- `tests/pop/fixtures/buck_expected.ic` — known-good converged ICs for
  the above, generated from a reference Xyce run.
- `tests/pop/fixtures/buck_ac.fd` — reference Xyce `.FD` output for the
  AC sweep, for `test_ac.py` to parse without needing Xyce installed.

Unit tests (`test_netlist.py`, `test_parse.py`) already pass with
synthetic data and no Xyce dependency.  Integration tests
(`test_runner_integration.py`) are gated on `pytest.mark.xyce` and
skipped in CI unless Xyce is on `PATH`.
