# LTspice → Xyce: state & handoff

Goal: run LTspice circuits through Xyce and validate against LTspice's own
output. This note is the map for picking the work up.

## Architecture (who does what)

| piece | repo / path | role |
|-------|-------------|------|
| **ltspice2xyce.pl** | `xyce/utils/ltspice2xyce.pl` | **canonical LTspice-netlist → Xyce translator** (streaming, in the `gnucap2xyce.pl`/`cadence2xyce.pl` family). All per-line rules live in `cir_to_xyce()`. Recurses into `.include/.inc/.lib`. CLI: `ltspice2xyce.pl [-o out] [--lib FILE] in.cir` |
| **ltz** | `ltz/bin/ltz` | LTspice-compatible CLI. `.asc`→`.cir` (`asc_to_netlist`), then **delegates `.cir`→Xyce to ltspice2xyce.pl** (`find_ltspice2xyce`), then runs Xyce, emits `.raw`. `ltz -b file` (run), `ltz -netlist file` (export only) |
| **LTspice shim** | `ltz/bin/LTspice` | drop-in: put `ltz/bin` ahead on PATH → `LTspice -b [-ascii] [-Run] file` simulates via Xyce |
| **regress block** | `sv2ghdl/regress` `Adapter::Ltz` | `ltz/circuits` (bundled `tests/ltspice_circuits`) + `ltz/community` (`../ltz-tests`). PASS = clean Xyce sim; compares the Xyce `.raw` vs an **LTspice gold** and reports the delta (analog: not gating) |
| **LTspice gold** | Wine | LTspice runs headless: `xvfb-run -a wine LTspice.exe -b -ascii circuit.net`. See the `ltspice-wine-gold` memory for the exact env (XDG_RUNTIME_DIR, WINEPREFIX=$HOME/ltwine, .net ext) |

`_read_raw`/`_compare_raw` in `Adapter::Ltz` parse both LTspice-ASCII and
Xyce-binary SPICE rawfiles (real + complex) and align by variable name.

## What works

`ltz/circuits` ~12/43 pass (was 5). Where ltz/Xyce simulates a circuit with
comparable nodes it **reproduces LTspice to ~0%** (RC filter, `opmodel_current`,
`CB_BJT_DC` 0.42%). Drop-in shim + standalone translator + the regression
comparison are all working end to end.

Translator rules already in `cir_to_xyce()` (the place to add more):
`.PROBE`/`.PLOT` removal, `.PLOT`→`.PRINT`, empty diode-model defaults,
`.param` space-form→`name=value` (`_ltspice_param_to_xyce`, comment/brace aware),
**recursive `.include/.lib` translation**, `.meas` commented out (Xyce `.MEASURE`
≠ LTspice `.meas`), `.func ^`→`**`, `.tran` single-arg/uic, British notation
(`4n7`), `Rser=` strip, `;`→`*`, `SINE`→`SIN`, µ→u, **`.LIB <file>`→`.INCLUDE`**
(sectionless), **keep only the last `.END`**. Also: ecircuit `.lib/.meas`
support files restored into the corpus (3 were UTF-16 → transcoded).

## What's left — by class (the 31 sim failures)

**Translator rules to add (clean wins, do these first):**
- **`.AC` + `.PRINT TRAN` inconsistent** (~2): the deck declares one analysis but
  `.PRINT`s another type. `analysis_type` is already tracked in pass 1 of
  `cir_to_xyce`; rewrite the `.PRINT <wrongtype>` to match it (or drop the type).
- **LTspice `if(cond,a,b)` ternary** (≥1): Xyce wants `IF(cond,a,b)` (and a real
  expression, not string compares like `IF(FILTER=,...)`). Translate `if`→`IF`
  and reject/skip string-compare forms.
- **Undefined params** (≥1, e.g. `Cannot convert 'VPEAK' to double`): a `.param`
  or `.step` value is referenced but never defined (often a `.step param` LTspice
  sweep that wasn't translated). Look at `.step` handling.
- **~16 uncategorized**: each needs `ltz -b <file>` then read the Xyce error;
  expect more of the same families. (Run the categorizer in the session log:
  loop the no-`.raw` set, grep `Netlist error:`.)

**Corpus issues (not translator bugs):**
- Misnamed include: decks `.include Mux___Behavioral.lib` but ecircuit ships
  `Mux_4_1_Behavioral.lib` (31_ADC, 34_MUX2X1). Fix the `.cir` reference or add
  an alias copy.
- No-analysis "library" decks (e.g. `29_OPAMP_ENCAPSULATION`): only `.SUBCKT`
  definitions, nothing to simulate → "No analysis specified" is correct; these
  aren't runnable tests.

**Divergences — run but don't match LTspice (reported, not failing):**
- `10_ABM/*` (`B`-source / tables): Xyce vs LTspice behavioral-source semantics
  differ — a model/expression problem, not a parse problem. Compare the netlists.
- `02_OPAMP_BASIC/opmodel1`: huge delta; the `.meas`-removal or a node/topology
  effect — diff the Xyce vs LTspice `.raw` by node.

**Comparison quality:**
- "no common variables" (`03_Mesh_MIX_CIRCUIT`, `34_MUX2X1`): ltz/Xyce and
  LTspice print different node names → nothing to compare. Improve node-name
  alignment in `Adapter::Ltz::_compare_raw` (or normalize `.PRINT` node names).

**Not re-baselined:** `ltz/community` (~369 `.asc`) — rerun now that the rules
landed.

## How to iterate

```
ltz -netlist foo.cir            # see the translated Xyce netlist
ltz -b foo.cir                  # translate + run Xyce (XYCE=build-area, LD_LIBRARY_PATH)
# add a rule: edit cir_to_xyce() in xyce/utils/ltspice2xyce.pl, re-test
cd sv2ghdl/regress && ./regress run ltz/circuits --filter <dir>   # harness + LTspice gold
```
Always use the **build-area** Xyce (`/usr/local/src/xyce-build/src/Xyce`,
`LD_LIBRARY_PATH` = that dir) per the project rule.
