#!/usr/bin/env python3
"""
ltspice2xyce.py — Convert LTspice netlists to Xyce-compatible format.

Handles the full set of LTspice→Xyce syntax differences discovered from
testing 69 Educational example circuits.
"""

import re
import sys
import os
import shutil
from pathlib import Path
from typing import List, Tuple, Optional


# LTspice standard library search paths
LTSPICE_DIR = Path.home() / ".wine/drive_c/Program Files/LTC/LTspiceXVII"
LTSPICE_LIBSUB = LTSPICE_DIR / "lib" / "sub"
LTSPICE_LIBCMP = LTSPICE_DIR / "lib" / "cmp"


def parse_eng(s: str) -> Optional[float]:
    """Parse engineering notation: 10ms → 0.01, 5k → 5000."""
    suffixes = {
        't': 1e12, 'g': 1e9, 'meg': 1e6, 'k': 1e3,
        'm': 1e-3, 'u': 1e-6, 'n': 1e-9, 'p': 1e-12, 'f': 1e-15,
    }
    m = re.match(r'^([+-]?[\d.]+(?:e[+-]?\d+)?)\s*(meg|[tgkmunpf])?(?:[a-z]*)?$', s, re.I)
    if m:
        num = float(m.group(1))
        suf = m.group(2)
        if suf:
            mult = suffixes.get(suf.lower())
            if mult:
                return num * mult
        return num
    return None


class LTspiceToXyce:
    """Stateful converter: one instance per netlist."""

    def __init__(self, outdir: str, asc_dir: str = ".", verbose: bool = False):
        self.outdir = Path(outdir)
        self.asc_dir = Path(asc_dir)
        self.verbose = verbose
        self.changes: List[str] = []
        self.warnings: List[str] = []
        self.vdmos_models: dict = {}  # model_name → va_module_name
        self._used_models: set = set()  # model names referenced by device lines

    def log(self, msg: str):
        if self.verbose:
            print(f"  {msg}", file=sys.stderr)

    def convert(self, lines: List[str]) -> List[str]:
        """Main conversion pipeline."""
        # Strip CR, fix encoding, sanitize device names
        lines = [l.replace('\r', '').replace('\xb5', 'u').replace('\xa7', '_') for l in lines]

        # Multi-pass conversion
        lines = self._remove_ltspice_directives(lines)
        lines = self._fix_params(lines)
        lines = self._fix_lib_includes(lines)
        lines = self._fix_sources(lines)
        lines = self._fix_rser(lines)
        lines = self._fix_tran(lines)
        lines = self._fix_monte_carlo(lines)
        lines = self._fix_step(lines)
        lines = self._fix_measure(lines)
        lines = self._fix_models(lines)
        lines = self._fix_op_print(lines)
        lines = self._fix_save(lines)
        # Pre-scan: collect model names referenced by M device lines
        for line in lines:
            m = re.match(r'^M\S*\s+\S+\s+\S+\s+\S+\s+\S+\s+(\S+)', line.strip(), re.I)
            if m:
                self._used_models.add(m.group(1).upper())
        lines = self._resolve_includes(lines)
        lines = self._rewrite_vdmos_devices(lines)
        lines = self._ensure_print(lines)
        lines = self._ensure_end(lines)
        return lines

    def _remove_ltspice_directives(self, lines):
        out = []
        for line in lines:
            s = line.strip()
            u = s.upper()
            if u == '.BACKANNO' or u.startswith('.BACKANNO '):
                self.changes.append("Removed .BACKANNO")
                continue
            if u == '.PROBE' or u.startswith('.PROBE '):
                self.changes.append("Removed .PROBE")
                continue
            # .options maxstep=X → .OPTIONS TIMEINT DELMAX=X
            m = re.match(r'^\.options\s+maxstep\s*=\s*(\S+)', s, re.I)
            if m:
                out.append(f'.OPTIONS TIMEINT DELMAX={m.group(1)}\n')
                self.changes.append(f".options maxstep → .OPTIONS TIMEINT DELMAX")
                continue
            # .options with other LTspice-specific options — pass through
            # (Xyce ignores unknown options gracefully)
            out.append(line)
        return out

    def _fix_params(self, lines):
        """Fix .params → .PARAM, reserved param names."""
        out = []
        for line in lines:
            s = line.strip()
            # .params → .PARAM
            if re.match(r'^\.params\b', s, re.I):
                line = re.sub(r'^\.params\b', '.PARAM', s, flags=re.I) + '\n'
                self.changes.append(".params → .PARAM")
            # freq is reserved in Xyce — rename to f_user
            if re.match(r'^\.param\s+freq\s*=', s, re.I):
                line = re.sub(r'\bfreq\b', 'f_user', s, flags=re.I) + '\n'
                self.changes.append("Renamed reserved param 'freq' → 'f_user'")
            out.append(line)
        # Also rename {freq} references in all lines
        has_freq_rename = any('f_user' in c for c in self.changes)
        if has_freq_rename:
            out = [re.sub(r'\{freq\}', '{f_user}', l, flags=re.I) for l in out]
        return out

    def _fix_lib_includes(self, lines):
        """.lib <file> → .INCLUDE <basename> (when no section name)."""
        out = []
        for line in lines:
            s = line.strip()
            m = re.match(r'^\.lib\s+(.+)$', s, re.I)
            if m:
                arg = m.group(1).strip()
                # .lib file section — keep as-is (has two args)
                parts = arg.split()
                if len(parts) >= 2 and not parts[-1].endswith(('.bjt', '.dio', '.mos',
                        '.jft', '.cap', '.ind', '.res', '.bead', '.sub', '.lib')):
                    # Looks like ".lib file section" — keep as .LIB
                    out.append(line)
                    continue
                # Single file reference — extract basename, convert to .INCLUDE
                basename = Path(arg.replace('\\', '/')).name
                out.append(f".INCLUDE {basename}\n")
                self.changes.append(f".lib → .INCLUDE {basename}")
            else:
                out.append(line)
        return out

    def _fix_sources(self, lines):
        """Fix source syntax: SINE→SIN, strip Rser= from sources."""
        out = []
        for line in lines:
            s = line.strip()

            # SINE( → SIN(
            if re.search(r'\bSINE\s*\(', s, re.I):
                s = re.sub(r'\bSINE\s*\(', 'SIN(', s, flags=re.I)
                self.changes.append("SINE → SIN")
                line = s + '\n'

            # Strip Rser= from V/I sources (LTspice inline series resistance)
            if re.match(r'^[VI]\S*\s', s, re.I) and re.search(r'\bRser=', s, re.I):
                s = re.sub(r'\s+Rser=\S+', '', s, flags=re.I)
                self.changes.append("Stripped Rser= from source")
                line = s + '\n'

            # Strip 'startup' keyword from source specifications
            if re.match(r'^[VI]\S*\s', s, re.I) and re.search(r'\bstartup\b', s, re.I):
                s = re.sub(r'\s+startup\b', '', s, flags=re.I)
                line = s + '\n'

            # wavefile= → comment out (unsupported)
            if re.search(r'\bwavefile=', s, re.I):
                out.append(f"* [ltz] unsupported: {s}\n")
                self.warnings.append(f"wavefile source unsupported: {s[:60]}")
                continue

            out.append(line)
        return out

    def _fix_rser(self, lines):
        """Convert Rser=/Lser=/Cpar= on L/C devices to explicit components.

        C1 out 0 10p Rser=100 Lser=1n  →  C1 out C1_r 10p
                                            R_C1_ser C1_r C1_l 100
                                            L_C1_ser C1_l 0 1n
        """
        out = []
        for line in lines:
            s = line.strip()
            # Match L or C device with inline parasitic params
            m = re.match(
                r'^([LC])(\S+)\s+(\S+)\s+(\S+)\s+(\S+)((?:\s+(?:Rser|Lser|Cpar|Rpar)=\S+)*)(.*)',
                s, re.I
            )
            if m and m.group(6):
                prefix = m.group(1)
                name = m.group(2)
                n1 = m.group(3)
                n2 = m.group(4)
                value = m.group(5)
                params_str = m.group(6)
                rest = m.group(7).strip()
                rest = re.sub(r'\s*noiseless\b', '', rest, flags=re.I)

                # Parse inline params
                rser = re.search(r'Rser=(\S+)', params_str, re.I)
                lser = re.search(r'Lser=(\S+)', params_str, re.I)
                cpar = re.search(r'Cpar=(\S+)', params_str, re.I)

                # Build chain: device → Rser → Lser → n2
                cur_node = n1
                next_id = 0

                def mid(suffix):
                    return f"{prefix}{name}_{suffix}"

                # Main device
                if rser or lser:
                    nxt = mid('r') if rser else (mid('l') if lser else n2)
                    out.append(f"{prefix}{name} {cur_node} {nxt} {value}")
                    if rest:
                        out[-1] += f" {rest}"
                    out[-1] += '\n'
                    cur_node = nxt
                else:
                    out.append(f"{prefix}{name} {cur_node} {n2} {value}")
                    if rest:
                        out[-1] += f" {rest}"
                    out[-1] += '\n'
                    cur_node = n2

                if rser:
                    nxt = mid('l') if lser else n2
                    out.append(f"R_{prefix}{name}_ser {cur_node} {nxt} {rser.group(1)}\n")
                    self.changes.append(f"Split Rser on {prefix}{name}")
                    cur_node = nxt

                if lser:
                    out.append(f"L_{prefix}{name}_ser {cur_node} {n2} {lser.group(1)}\n")
                    self.changes.append(f"Split Lser on {prefix}{name}")

                if cpar:
                    out.append(f"C_{prefix}{name}_par {n1} {n2} {cpar.group(1)}\n")
                    self.changes.append(f"Split Cpar on {prefix}{name}")
            else:
                out.append(line)
        return out

    def _fix_tran(self, lines):
        """Fix .tran: single-arg, strip startup/uic."""
        out = []
        for line in lines:
            s = line.strip()
            if not re.match(r'^\.tran\b', s, re.I):
                out.append(line)
                continue

            # Strip startup/uic keywords
            modified = re.sub(r'\s+(?:startup|uic)\b', '', s, flags=re.I)
            if modified != s:
                self.changes.append("Stripped startup/uic from .tran")
                s = modified

            # Parse fields
            fields = s.split()
            if len(fields) == 2:
                # Single arg: .tran Tstop → .TRAN 0 Tstop
                tstop = fields[1]
                out.append(f".TRAN 0 {tstop}\n")
                self.changes.append(f".tran {tstop} → .TRAN 0 {tstop}")
            elif len(fields) >= 3:
                # Check if tstep=0
                tstep = parse_eng(fields[1])
                if tstep is not None and tstep == 0:
                    out.append(f".TRAN 0 {' '.join(fields[2:])}\n")
                else:
                    out.append(s + '\n')
            else:
                out.append(s + '\n')
        return out

    def _fix_monte_carlo(self, lines):
        """Convert LTspice mc()/flat()/gauss() → Xyce AUNIF/AGAUSS/RAND.

        LTspice mc(val, tol) = uniform random in [val*(1-tol), val*(1+tol)]
                             → Xyce AUNIF(val, val*tol)
        LTspice flat(x)      = uniform random in [-x, x]
                             → Xyce (2*RAND()-1)*x   or  AUNIF(0, x)
        LTspice gauss(sigma)  = gaussian with sigma
                             → Xyce AGAUSS(0, sigma, 1)

        Also converts the dummy .step loop to .SAMPLING + .options SAMPLES.
        """
        has_mc = any(re.search(r'\bmc\s*\(', l, re.I) for l in lines)
        has_flat = any(re.search(r'\bflat\s*\(', l, re.I) for l in lines)
        has_gauss = any(re.search(r'\bgauss\s*\(', l, re.I) for l in lines)

        if not (has_mc or has_flat or has_gauss):
            return lines

        # Count MC iterations from dummy .step
        num_samples = 20  # default
        out = []
        for line in lines:
            s = line.strip()

            # Detect dummy .step param X for MC cycling
            m = re.match(r'^\.step\s+param\s+(\w+)\s+(\S+)\s+(\S+)\s+(\S+)', s, re.I)
            if m and (has_mc or has_flat or has_gauss):
                param = m.group(1)
                start = parse_eng(m.group(2))
                stop = parse_eng(m.group(3))
                step = parse_eng(m.group(4))
                if start is not None and stop is not None and step is not None and step > 0:
                    num_samples = int((stop - start) / step) + 1
                # Comment out the dummy .step and add .SAMPLING
                out.append(f"* [ltz] MC: replaced .step with .SAMPLING ({num_samples} samples)\n")
                out.append(f".options SAMPLES numsamples={num_samples}\n")
                self.changes.append(f".step MC loop → .options SAMPLES numsamples={num_samples}")
                continue

            # mc(val, tol) → AUNIF(val, val*tol)
            def mc_repl(m):
                val = m.group(1).strip()
                tol = m.group(2).strip()
                return f"AUNIF({val}, {val}*{tol})"

            if re.search(r'\bmc\s*\(', s, re.I):
                s = re.sub(r'\bmc\s*\(\s*([^,]+)\s*,\s*([^)]+)\s*\)', mc_repl, s, flags=re.I)
                self.changes.append("mc() → AUNIF()")
                line = s + '\n'

            # flat(x) → AUNIF(0, x)
            if re.search(r'\bflat\s*\(', s, re.I):
                s = re.sub(r'\bflat\s*\(\s*([^)]+)\s*\)', r'AUNIF(0, \1)', s, flags=re.I)
                self.changes.append("flat() → AUNIF(0, x)")
                line = s + '\n'

            # gauss(sigma) → AGAUSS(0, sigma, 1)
            if re.search(r'\bgauss\s*\(', s, re.I):
                s = re.sub(r'\bgauss\s*\(\s*([^)]+)\s*\)', r'AGAUSS(0, \1, 1)', s, flags=re.I)
                self.changes.append("gauss() → AGAUSS()")
                line = s + '\n'

            out.append(line)

        return out

    def _fix_step(self, lines):
        """Convert .step list → Xyce .STEP DATA table.
        Also auto-declare .PARAM for stepped params if not already declared.
        """
        out = []
        i = 0
        data_tables = []  # collect (table_name, param_name, values)
        stepped_params = []  # params that need .PARAM declaration

        # Collect existing .param names
        existing_params = set()
        for line in lines:
            m = re.match(r'^\s*\.param\s+(\w+)', line, re.I)
            if m:
                existing_params.add(m.group(1).upper())

        while i < len(lines):
            s = lines[i].strip()
            m = re.match(r'^\.step\s+(?:param\s+)?(\S+)\s+list\s+(.+)$', s, re.I)
            if m:
                param = m.group(1)
                values = m.group(2).split()
                table_name = f"ltz_{param}"
                data_tables.append((table_name, param, values))
                stepped_params.append((param, values[0]))
                out.append(f".STEP DATA={table_name}\n")
                self.changes.append(f".step list → .STEP DATA for {param}")
                i += 1
                continue

            # .step oct → .STEP DEC (Xyce uses DEC for logarithmic)
            m = re.match(r'^\.step\s+oct\s+(?:param\s+)?(\S+)\s+(\S+)\s+(\S+)\s+(\S+)', s, re.I)
            if m:
                param, start, stop, npts = m.group(1), m.group(2), m.group(3), m.group(4)
                stepped_params.append((param, start))
                out.append(f".STEP DEC {param} {start} {stop} {npts}\n")
                self.changes.append(f".step oct → .STEP DEC for {param}")
                i += 1
                continue

            # .step param X start stop step → .STEP LIN X start stop step
            m = re.match(r'^\.step\s+param\s+(\S+)\s+(\S+)\s+(\S+)\s+(\S+)\s*$', s, re.I)
            if m:
                param, start, stop, step = m.group(1), m.group(2), m.group(3), m.group(4)
                stepped_params.append((param, start))
                out.append(f".STEP LIN {param} {start} {stop} {step}\n")
                self.changes.append(f".step param → .STEP LIN for {param}")
                i += 1
                continue

            # .step NPN model(param) — model parameter stepping (unsupported, comment out)
            m = re.match(r'^\.step\s+(NPN|PNP|NMOS|PMOS|D)\s+\S+\(\S+\)', s, re.I)
            if m:
                out.append(f"* [ltz] unsupported: {s}\n")
                self.warnings.append(f"Model param stepping unsupported: {s[:60]}")
                i += 1
                continue

            # .step temp → .STEP TEMP
            m = re.match(r'^\.step\s+temp\s+(\S+)\s+(\S+)\s+(\S+)\s*$', s, re.I)
            if m:
                start, stop, step = m.group(1), m.group(2), m.group(3)
                out.append(f".STEP LIN TEMP {start} {stop} {step}\n")
                self.changes.append(f".step temp → .STEP LIN TEMP")
                i += 1
                continue

            out.append(lines[i])
            i += 1

        # Auto-declare .PARAM for stepped params not already declared
        param_inserts = []
        for param, default_val in stepped_params:
            if param.upper() not in existing_params and param.upper() != 'TEMP':
                param_inserts.append(f".PARAM {param} = {default_val}\n")
                self.changes.append(f"Auto-declared .PARAM {param} for .STEP")

        if param_inserts:
            # Insert after title line (line 0)
            out[1:1] = param_inserts

        # Append DATA tables at the end (before .end)
        if data_tables:
            end_idx = len(out)
            for j in range(len(out) - 1, -1, -1):
                if out[j].strip().upper() == '.END':
                    end_idx = j
                    break

            inserts = []
            for table_name, param, values in data_tables:
                inserts.append(f".DATA {table_name}\n")
                inserts.append(f"+ {param}\n")
                for v in values:
                    inserts.append(f"+ {v}\n")
                inserts.append(f".ENDDATA\n")

            out[end_idx:end_idx] = inserts

        return out

    def _fix_measure(self, lines):
        """.meas → .MEASURE, add analysis type if missing."""
        out = []
        # Detect analysis type
        analysis = None
        for line in lines:
            u = line.strip().upper()
            if u.startswith('.TRAN'): analysis = 'TRAN'
            elif u.startswith('.AC'): analysis = 'AC'
            elif u.startswith('.DC'): analysis = 'DC'
            elif u.startswith('.NOISE'): analysis = 'NOISE'

        for line in lines:
            s = line.strip()
            if re.match(r'^\.meas\b', s, re.I):
                # Convert .meas → .MEASURE
                s = re.sub(r'^\.meas\b', '.MEASURE', s, flags=re.I)

                # Check if analysis type is present
                after = re.sub(r'^\.MEASURE\s+', '', s)
                first_word = after.split()[0].upper() if after.split() else ''
                if first_word not in ('TRAN', 'AC', 'DC', 'NOISE', 'OP'):
                    if analysis:
                        s = f".MEASURE {analysis} {after}"
                        self.changes.append(f"Added {analysis} to .MEASURE")

                out.append(s + '\n')
                self.changes.append(".meas → .MEASURE")
            else:
                out.append(line)
        return out

    def _generate_vdmos_va(self, name: str, params: dict, pchan: bool) -> str:
        """Generate a Verilog-A VDMOS model from parsed LTspice parameters."""
        vto = abs(params.get('vto', 2.0))
        kp = params.get('kp', 10.0)
        lam = params.get('lambda', 0.01)
        rd = params.get('rd', 0.0)
        rs = params.get('rs', 0.0)
        rg = params.get('rg', 0.0)
        mtriode = params.get('mtriode', 1.0)
        ksubthres = params.get('ksubthres', 0.1)
        cgs = params.get('cgs', 0.0)
        cgdmin = params.get('cgdmin', 0.0)
        cgdmax = params.get('cgdmax', 0.0)
        a_cgd = params.get('a', 0.5)
        cjo = params.get('cjo', 0.0)
        is_diode = params.get('is', 1e-14)
        n_diode = params.get('n', 1.0)
        sign = "-" if pchan else ""

        return f"""`include "disciplines.vams"
`include "constants.vams"
module {name}(d, g, s);
    inout d, g, s;
    electrical d, g, s;
    parameter real Vto={vto}; parameter real Kp={kp}; parameter real Lambda={lam};
    parameter real Mtriode={mtriode}; parameter real Ksubthres={ksubthres};
    parameter real Cgs_val={cgs}; parameter real Cgdmin={cgdmin};
    parameter real Cgdmax={cgdmax}; parameter real A_cgd={a_cgd};
    parameter real Cjo_val={cjo}; parameter real Is_val={is_diode}; parameter real N_val={n_diode};
    real Vgs, Vds, Ids, Vov, Cgd_val;
    analog begin
        Vgs = {"V(s, g)" if pchan else "V(g, s)"};
        Vds = {"V(s, d)" if pchan else "V(d, s)"};
        Vov = Vgs - Vto;
        if (Vov <= 0.0) begin
            if (Ksubthres > 0.0)
                Ids = Kp * Ksubthres * Ksubthres * ln(1.0 + exp(Vov / Ksubthres)) * ln(1.0 + exp(Vov / Ksubthres));
            else Ids = 0.0;
        end else if (Vds < Vov) begin
            Ids = Kp * (Vov * Vds - 0.5 * pow(Vds, Mtriode) * pow(Vov, 2.0 - Mtriode)) * (1.0 + Lambda * Vds);
        end else begin
            Ids = 0.5 * Kp * Vov * Vov * (1.0 + Lambda * Vds);
        end
        I(d, s) <+ {sign}Ids;
        I(s, d) <+ Is_val * (limexp(V(s, d) / (N_val * $vt)) - 1.0);
        I(g, s) <+ Cgs_val * ddt(V(g, s));
        Cgd_val = Cgdmin + (Cgdmax - Cgdmin) / (1.0 + A_cgd * max(0.0, Vds));
        I(g, d) <+ Cgd_val * ddt(V(g, d));
        I(d, s) <+ Cjo_val * ddt(V(d, s));
    end
endmodule
"""

    def _fix_models(self, lines):
        """Fix model syntax: empty .model D D, LPNP→PNP, VDMOS→VA, SW→switch."""
        out = []
        vdmos_vas = {}  # name → va_content
        for line in lines:
            s = line.strip()

            # .model X VDMOS(...) → generate Verilog-A, replace with .hdl
            m = re.match(r'^\.model\s+(\S+)\s+VDMOS\s*\((.+)\)\s*$', s, re.I)
            if m:
                mname = m.group(1)
                params_str = m.group(2)
                pchan = 'pchan' in params_str.lower()
                params = {}
                for pm in re.finditer(r'(\w+)\s*=\s*([^\s,]+)', params_str):
                    k = pm.group(1).lower()
                    try:
                        params[k] = parse_eng(pm.group(2))
                    except (ValueError, TypeError):
                        params[k] = pm.group(2)
                # Use a sanitized module name (no special chars)
                va_name = f"ltz_vdmos_{re.sub(r'[^a-zA-Z0-9_]', '_', mname)}"
                va_content = self._generate_vdmos_va(va_name, params, pchan)
                va_path = Path(self.outdir) / f"{va_name}.va"
                va_path.write_text(va_content)
                vdmos_vas[mname] = va_name
                self.vdmos_models[mname.upper()] = va_name
                out.append(f'.hdl "{(Path(self.outdir) / (va_name + ".va")).resolve()}"\n')
                out.append(f'.model {mname} {va_name}\n')
                self.changes.append(f"VDMOS {mname} → Verilog-A {va_name}.va")
                continue

            # .model D D (empty) → add default params
            if re.match(r'^\.model\s+D\s+D\s*$', s, re.I):
                out.append('.model D D(IS=2.52e-9 RS=0.568 N=1.752 BV=100 IBV=100u)\n')
                self.changes.append("Added default params to empty diode model")
                continue

            # .model X LPNP(...) → .model X PNP(...)
            # LPNP is LTspice-specific lateral PNP — map to standard PNP
            if re.match(r'^\.model\s+\S+\s+LPNP\b', s, re.I):
                s = re.sub(r'\bLPNP\b', 'PNP', s, flags=re.I)
                self.changes.append("LPNP → PNP")
                out.append(s + '\n')
                continue

            # .model X NJF/PJF (empty) → add default params
            if re.match(r'^\.model\s+\S+\s+(NJF|PJF)\s*$', s, re.I):
                jtype = 'NJF' if 'NJF' in s.upper() else 'PJF'
                name = s.split()[1]
                out.append(f'.model {name} {jtype}(VTO=-2 BETA=1e-4 LAMBDA=0.01)\n')
                self.changes.append(f"Added default params to empty {jtype} model")
                continue

            # .model X NPN/PNP (empty, no params) → add minimal defaults
            m = re.match(r'^\.model\s+(\S+)\s+(NPN|PNP)\s*$', s, re.I)
            if m:
                name, btype = m.group(1), m.group(2).upper()
                out.append(f'.model {name} {btype}(BF=100 IS=1e-14)\n')
                self.changes.append(f"Added default params to empty {btype} model")
                continue

            # Strip 'noiseless' keyword from model params
            if ' noiseless' in s.lower():
                s = re.sub(r'\s*noiseless\b', '', s, flags=re.I)
                line = s + '\n'

            out.append(line)
        return out

    def _fix_op_print(self, lines):
        """.OP + .PRINT OP → .OP + .PRINT DC (Xyce inconsistency workaround)."""
        has_op = any(re.match(r'^\s*\.op\b', l, re.I) for l in lines)
        if not has_op:
            return lines

        out = []
        for line in lines:
            s = line.strip()
            if re.match(r'^\.PRINT\s+OP\b', s, re.I):
                # Replace .PRINT OP with .PRINT DC
                s = re.sub(r'^\.PRINT\s+OP\b', '.PRINT DC', s, flags=re.I)
                self.changes.append(".PRINT OP → .PRINT DC (for .OP analysis)")
                out.append(s + '\n')
            else:
                out.append(line)
        return out

    def _fix_save(self, lines):
        """.save → comment out (Xyce uses .PRINT)."""
        out = []
        for line in lines:
            if re.match(r'^\s*\.save\b', line, re.I):
                out.append(f"* [ltz] removed: {line.strip()}\n")
                self.changes.append("Removed .save")
            else:
                out.append(line)
        return out

    def _resolve_includes(self, lines):
        """Copy .INCLUDE'd files to outdir, sanitize them."""
        for line in lines:
            m = re.match(r'^\s*\.(?:include|INCLUDE)\s+(\S+)', line)
            if not m:
                continue
            fname = m.group(1).strip('"')
            basename = Path(fname.replace('\\', '/')).name
            dest = self.outdir / basename

            if dest.exists():
                continue

            found = self._find_include(basename)
            if found:
                self._copy_and_sanitize_lib(found, dest)

        return lines

    def _find_include(self, basename: str) -> Optional[Path]:
        """Search LTspice directories for an include file."""
        candidates = [
            self.asc_dir / basename,
            LTSPICE_LIBSUB / basename,
            LTSPICE_LIBCMP / basename,
        ]
        for p in candidates:
            if p.exists():
                return p
        # Recursive search in lib/sub
        for p in LTSPICE_LIBSUB.rglob(basename):
            return p
        return None

    def _copy_and_sanitize_lib(self, src: Path, dest: Path):
        """Copy a library file, fixing LTspice-specific syntax."""
        try:
            content = src.read_bytes().decode('latin-1')
        except Exception:
            shutil.copy2(src, dest)
            return

        content = content.replace('\r', '')
        content = content.replace('\xb5', 'u')
        content = content.replace('\xa7', '_')

        # Strip noiseless keyword
        content = re.sub(r'\s+noiseless\b', '', content, flags=re.I)

        # Remove ako: lines (LTspice "A Kind Of" model inheritance)
        content = re.sub(r'^.*\bako:.*$', '', content, flags=re.M | re.I)

        # LPNP → PNP
        content = re.sub(r'\bLPNP\b', 'PNP', content, flags=re.I)

        # .params → .PARAM
        content = re.sub(r'^\.params\b', '.PARAM', content, flags=re.M | re.I)

        # Reserved param name: freq → f_user
        content = re.sub(r'\bfreq\b', 'f_user', content)

        # Empty models
        content = re.sub(r'^(\.model\s+\S+\s+NPN)\s*$', r'\1(BF=100 IS=1e-14)',
                         content, flags=re.M | re.I)
        content = re.sub(r'^(\.model\s+\S+\s+PNP)\s*$', r'\1(BF=100 IS=1e-14)',
                         content, flags=re.M | re.I)

        # Fix commas in .model params (LTspice allows, Xyce doesn't)
        content = re.sub(r'(\.\s*model\s+\S+\s+\w+\([^)]*),([^)]*\))',
                         lambda m: m.group(0).replace(',', ' '),
                         content, flags=re.I)

        # VDMOS → generate Verilog-A files and replace .model lines
        lines = content.split('\n')
        out_lines = []
        hdl_lines = []  # .hdl directives to prepend
        for line in lines:
            # Join continuation lines for .model matching
            m = re.match(r'^\.model\s+(\S+)\s+VDMOS\b', line, re.I)
            if m:
                mname = m.group(1)
                if mname.upper() not in self._used_models:
                    out_lines.append(f'* [ltz] unused VDMOS: {mname}')
                    continue
                # Extract params from the full line
                pm = re.match(r'^\.model\s+\S+\s+VDMOS\s*\((.+)\)', line, re.I)
                if not pm:
                    out_lines.append(f'* [ltz] malformed VDMOS: {mname}')
                    continue
                params_str = pm.group(1)
                pchan = 'pchan' in params_str.lower()
                params = {}
                for pm in re.finditer(r'(\w+)\s*=\s*([^\s,)]+)', params_str):
                    k = pm.group(1).lower()
                    if k in ('pchan', 'mfg'):
                        continue
                    try:
                        params[k] = parse_eng(pm.group(2))
                    except (ValueError, TypeError):
                        pass
                va_name = f"ltz_vdmos_{re.sub(r'[^a-zA-Z0-9_]', '_', mname)}"
                va_content = self._generate_vdmos_va(va_name, params, pchan)
                va_path = self.outdir / f"{va_name}.va"
                va_path.write_text(va_content)
                hdl_lines.append(f'.hdl "{va_path.resolve()}"')
                out_lines.append(f'.model {mname} {va_name}')
                self.changes.append(f"VDMOS {mname} → VA {va_name}.va (in lib)")
            else:
                out_lines.append(line)

        # Prepend .hdl directives at the top of the file
        if hdl_lines:
            out_lines = hdl_lines + out_lines

        dest.write_text('\n'.join(out_lines))

    def _rewrite_vdmos_devices(self, lines):
        """Rewrite M device lines that reference VDMOS models to Y device lines.

        M_Q1 +V N010 N012 N012 IRFP240  →  Yltz_vdmos_IRFP240 Q1 +V N010 N012 N012 IRFP240
        """
        if not self.vdmos_models:
            # Also check included files for VDMOS models
            for line in lines:
                m = re.match(r'^\s*\.include\s+(\S+)', line, re.I)
                if m:
                    fname = m.group(1).strip('"')
                    lib_path = self.outdir / Path(fname).name
                    if lib_path.exists():
                        content = lib_path.read_text()
                        for lm in re.finditer(
                                r'^\.model\s+(\S+)\s+(ltz_vdmos_\S+)',
                                content, re.M | re.I):
                            self.vdmos_models[lm.group(1).upper()] = lm.group(2)

        if not self.vdmos_models:
            return lines

        out = []
        for line in lines:
            s = line.strip()
            # Match M device lines: M<name> n1 n2 n3 [n4] modelname [params]
            m = re.match(r'^M[\w_]*\s+', s, re.I)
            if m:
                parts = s.split()
                inst = parts[0]  # M_Q1 or MQ1
                # Find the model name (first non-node, non-param token after nodes)
                # MOSFET has 4 nodes: d g s b
                if len(parts) >= 6:
                    model = parts[5] if not '=' in parts[5] else parts[4]
                    model_upper = model.upper()
                    if model_upper in self.vdmos_models:
                        va_mod = self.vdmos_models[model_upper]
                        # Strip M prefix for Y instance name
                        yname = re.sub(r'^M[_]?', '', inst, flags=re.I) or inst
                        nodes = ' '.join(parts[1:5])
                        params = ' '.join(parts[6:]) if len(parts) > 6 else ''
                        params_str = f' {params}' if params else ''
                        line = f'Y{va_mod.upper()} {yname} {nodes} {model}{params_str}\n'
                        self.changes.append(f"VDMOS device {inst} → Y{va_mod.upper()}")
            out.append(line)
        return out

    def _ensure_print(self, lines):
        """Add .PRINT if missing, or fix mismatched .PRINT type."""
        # Detect analysis type
        analysis = None
        for line in lines:
            u = line.strip().upper()
            for a in ('TRAN', 'AC', 'DC', 'NOISE'):
                if re.match(rf'^\s*\.{a}\b', u):
                    analysis = a
                    break
            if re.match(r'^\s*\.OP\b', u):
                analysis = 'DC'
            if re.match(r'^\s*\.TF\b', u):
                analysis = 'DC'

        if not analysis:
            return lines

        # Check existing .PRINT lines
        has_matching_print = False
        out = []
        for line in lines:
            m = re.match(r'^(\s*\.PRINT\s+)(\S+)(.*)', line, re.I)
            if m:
                print_type = m.group(2).upper()
                if print_type != analysis:
                    # Fix mismatched print type
                    line = f"{m.group(1)}{analysis}{m.group(3)}\n"
                    self.changes.append(f".PRINT {print_type} → .PRINT {analysis}")
                has_matching_print = True
            out.append(line)

        if has_matching_print:
            return out

        # No .PRINT at all — add one before .end
        result = []
        for line in out:
            if line.strip().upper() == '.END':
                result.append(f'.PRINT {analysis} FORMAT=RAW V(*)\n')
            result.append(line)
        return result

    def _ensure_end(self, lines):
        """Ensure .END exists."""
        has_end = any(l.strip().upper() == '.END' for l in lines)
        if not has_end:
            lines.append('.END\n')
        return lines


def convert_file(net_path: str, outdir: str, asc_dir: str = ".",
                 verbose: bool = False) -> Tuple[str, List[str], List[str]]:
    """Convert a single .net file. Returns (output_path, changes, warnings)."""
    net = Path(net_path)
    out = Path(outdir)
    out.mkdir(parents=True, exist_ok=True)

    lines = Path(net_path).read_bytes().decode('latin-1').splitlines(keepends=True)

    converter = LTspiceToXyce(outdir=str(out), asc_dir=asc_dir, verbose=verbose)
    result = converter.convert(lines)

    cir_path = out / f"{net.stem}.cir"
    cir_path.write_text(''.join(result))

    return str(cir_path), converter.changes, converter.warnings


def main():
    import argparse
    parser = argparse.ArgumentParser(description='LTspice netlist → Xyce converter')
    parser.add_argument('input', help='Input .net/.cir file')
    parser.add_argument('-o', '--outdir', default='.', help='Output directory')
    parser.add_argument('-d', '--asc-dir', default='.', help='Directory containing .asc (for resolving includes)')
    parser.add_argument('-v', '--verbose', action='store_true')
    args = parser.parse_args()

    cir, changes, warnings = convert_file(args.input, args.outdir, args.asc_dir, args.verbose)
    print(f"Output: {cir}")
    if changes:
        print(f"Changes ({len(changes)}):")
        for c in changes:
            print(f"  - {c}")
    if warnings:
        print(f"Warnings ({len(warnings)}):")
        for w in warnings:
            print(f"  ! {w}")


if __name__ == '__main__':
    main()
