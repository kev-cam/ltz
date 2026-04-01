#!/usr/bin/env python3
"""
devchar.py — Device characterization database builder.

Extracts IV/CV curves from any SPICE simulator and stores them in a
portable SQLite database.  From the database, generates Verilog-A models
that reproduce the device behavior in any simulator with VA support.

Supported simulators (as extraction sources):
  - LTspice (via wine64 + xvfb-run)
  - Xyce
  - ngspice
  - (extensible: add a SimRunner subclass)

Usage:
  # Extract a VDMOS model from LTspice standard library
  devchar extract --model Si7336ADP --type vdmos --sim ltspice

  # Extract all VDMOS from standard.mos
  devchar extract-lib standard.mos --type vdmos --sim ltspice

  # List extracted models
  devchar list

  # Generate Verilog-A for a model
  devchar emit-va --model Si7336ADP -o Si7336ADP.va

  # Dump IV data as CSV
  devchar dump --model Si7336ADP --sweep ids_vds
"""

import argparse
import json
import os
import re
import shutil
import sqlite3
import subprocess
import struct
import sys
import tempfile
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple


# ── Database ──────────────────────────────────────────────────────────

DB_PATH = Path(os.environ.get('DEVCHAR_DB',
    Path.home() / '.local/share/ltz/devchar.db'))


def get_db() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(str(DB_PATH))
    db.execute("PRAGMA journal_mode=WAL")
    db.executescript("""
        CREATE TABLE IF NOT EXISTS models (
            name       TEXT PRIMARY KEY,
            dev_type   TEXT NOT NULL,       -- vdmos, bjt, diode, switch, ...
            polarity   TEXT DEFAULT 'n',    -- n, p
            params     TEXT,                -- original model params as JSON
            source_sim TEXT,                -- ltspice, xyce, ngspice, ...
            extracted  TEXT,                -- ISO timestamp
            notes      TEXT
        );
        CREATE TABLE IF NOT EXISTS sweeps (
            id         INTEGER PRIMARY KEY,
            model      TEXT NOT NULL REFERENCES models(name),
            sweep_type TEXT NOT NULL,       -- ids_vds, ids_vgs, cgg_vgs, ...
            conditions TEXT,                -- JSON: fixed bias conditions
            n_points   INTEGER,
            data       BLOB,               -- packed float64 arrays
            columns    TEXT,                -- JSON: column names
            UNIQUE(model, sweep_type, conditions)
        );
    """)
    return db


# ── Model Card Parser ─────────────────────────────────────────────────

@dataclass
class ModelCard:
    name: str
    dev_type: str           # VDMOS, NPN, PNP, D, NMOS, PMOS, ...
    polarity: str = 'n'     # n or p
    params: Dict = field(default_factory=dict)
    raw_line: str = ''

    @staticmethod
    def parse_spice_lib(text: str) -> List['ModelCard']:
        """Parse .model statements from SPICE library text."""
        models = []
        # Join continuation lines
        text = re.sub(r'\n\+', ' ', text)
        for line in text.split('\n'):
            line = line.strip()
            if not line or line.startswith('*'):
                continue
            m = re.match(
                r'^\.model\s+(\S+)\s+(\w+)\s*\(?(.*?)\)?\s*$',
                line, re.I
            )
            if not m:
                continue
            name = m.group(1)
            dtype = m.group(2).upper()
            params_str = m.group(3)

            # Detect polarity
            polarity = 'n'
            if dtype in ('PNP', 'PMOS', 'PJF'):
                polarity = 'p'
            if 'pchan' in params_str.lower():
                polarity = 'p'

            # Parse key=value pairs
            params = {}
            for pm in re.finditer(r'(\w+)\s*=\s*(\S+)', params_str):
                key = pm.group(1).lower()
                val = pm.group(2)
                # Strip trailing non-numeric (like manufacturer names)
                try:
                    params[key] = _parse_eng(val)
                except (ValueError, TypeError):
                    params[key] = val

            models.append(ModelCard(
                name=name, dev_type=dtype, polarity=polarity,
                params=params, raw_line=line
            ))
        return models


def _parse_eng(s: str) -> float:
    """Parse engineering notation."""
    s = s.strip().rstrip(')')
    suffixes = {
        'T': 1e12, 'G': 1e9, 'MEG': 1e6, 'K': 1e3,
        'M': 1e-3, 'U': 1e-6, 'N': 1e-9, 'P': 1e-12, 'F': 1e-15,
    }
    m = re.match(r'^([+-]?[\d.]+(?:e[+-]?\d+)?)\s*(meg|[tgkmunpf])?',
                 s, re.I)
    if m:
        num = float(m.group(1))
        suf = m.group(2)
        if suf:
            mult = suffixes.get(suf.upper())
            if mult:
                return num * mult
        return num
    return float(s)


# ── Simulator Runners ─────────────────────────────────────────────────

class SimRunner(ABC):
    """Abstract base for running SPICE simulations."""

    @abstractmethod
    def run(self, netlist: str, workdir: str) -> Optional[str]:
        """Run netlist, return path to .raw file or None on failure."""
        ...

    @abstractmethod
    def name(self) -> str: ...


class LTspiceRunner(SimRunner):
    """Run LTspice via wine64 under xvfb."""

    def __init__(self):
        self.exe = (Path.home() / ".wine/drive_c/Program Files/LTC/"
                    "LTspiceXVII/XVIIx64.exe")

    def name(self) -> str:
        return "ltspice"

    def run(self, netlist: str, workdir: str) -> Optional[str]:
        cir_path = Path(workdir) / "sweep.cir"
        cir_path.write_text(netlist)

        # Convert to Windows path
        try:
            win_path = subprocess.check_output(
                ['winepath', '-w', str(cir_path)],
                stderr=subprocess.DEVNULL
            ).decode().strip()
        except Exception:
            win_path = str(cir_path)

        # Run LTspice -Run, poll for .raw, kill
        raw_path = cir_path.with_suffix('.raw')
        raw_path.unlink(missing_ok=True)

        proc = subprocess.Popen(
            ['xvfb-run', '-a', 'wine64', str(self.exe), '-Run', win_path],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
        )

        # Poll for .raw completion
        for _ in range(120):  # 120 seconds max
            time.sleep(1)
            if raw_path.exists():
                sz1 = raw_path.stat().st_size
                time.sleep(0.5)
                sz2 = raw_path.stat().st_size
                if sz1 == sz2 and sz1 > 0:
                    proc.kill()
                    proc.wait()
                    return str(raw_path)
            if proc.poll() is not None:
                break

        proc.kill()
        proc.wait()
        return str(raw_path) if raw_path.exists() else None


class XyceRunner(SimRunner):
    """Run Xyce natively."""

    def __init__(self):
        self.exe = os.environ.get('XYCE',
            '/usr/local/src/Xyce-8/xyce-build/src/Xyce')

    def name(self) -> str:
        return "xyce"

    def run(self, netlist: str, workdir: str) -> Optional[str]:
        cir_path = Path(workdir) / "sweep.cir"
        raw_path = Path(workdir) / "sweep.raw"
        cir_path.write_text(netlist)

        result = subprocess.run(
            [self.exe, '-r', str(raw_path), str(cir_path)],
            capture_output=True, timeout=120
        )
        if result.returncode == 0 and raw_path.exists():
            return str(raw_path)
        return None


class NgspiceRunner(SimRunner):
    """Run ngspice in batch mode."""

    def name(self) -> str:
        return "ngspice"

    def run(self, netlist: str, workdir: str) -> Optional[str]:
        cir_path = Path(workdir) / "sweep.cir"
        raw_path = Path(workdir) / "sweep.raw"
        cir_path.write_text(netlist)

        result = subprocess.run(
            ['ngspice', '-b', '-r', str(raw_path), str(cir_path)],
            capture_output=True, timeout=120
        )
        if raw_path.exists():
            return str(raw_path)
        return None


RUNNERS = {
    'ltspice': LTspiceRunner,
    'xyce': XyceRunner,
    'ngspice': NgspiceRunner,
}


# ── Raw File Parser ───────────────────────────────────────────────────

def parse_raw(path: str) -> Tuple[List[str], List[List[float]]]:
    """Parse a SPICE .raw file (binary or ASCII).

    Returns (column_names, data_rows).
    """
    with open(path, 'rb') as f:
        header = b''
        while True:
            line = f.readline()
            if not line:
                break
            header += line
            # Detect UTF-16LE (LTspice)
            if line[:2] == b'\xff\xfe' or (len(line) > 1 and line[1:2] == b'\x00'):
                return _parse_raw_ltspice(path)
            if b'Binary:' in line or b'binary:' in line.lower():
                return _parse_raw_binary(f, header)
            if b'Values:' in line or b'values:' in line.lower():
                return _parse_raw_ascii(f, header)

    # Try as plain ASCII with header
    return _parse_raw_ascii_full(path)


def _parse_raw_ltspice(path: str) -> Tuple[List[str], List[List[float]]]:
    """Parse LTspice UTF-16LE binary .raw file."""
    with open(path, 'rb') as f:
        raw = f.read()

    # LTspice raw is UTF-16LE header + binary data
    # Find the header/data boundary
    # Header ends with "Binary:\n" in UTF-16LE
    marker = 'Binary:\n'.encode('utf-16-le')
    idx = raw.find(marker)
    if idx < 0:
        # Try ASCII marker
        marker = b'Binary:\n'
        idx = raw.find(marker)
        if idx < 0:
            raise ValueError("Cannot find Binary: marker in LTspice .raw")

    header_bytes = raw[:idx]
    data_bytes = raw[idx + len(marker):]

    # Decode header
    try:
        header = header_bytes.decode('utf-16-le')
    except Exception:
        header = header_bytes.decode('latin-1')

    # Parse header
    n_vars = 0
    n_points = 0
    columns = []
    is_complex = False

    for line in header.split('\n'):
        line = line.strip()
        if line.startswith('No. Variables:'):
            n_vars = int(line.split(':')[1].strip())
        elif line.startswith('No. Points:'):
            n_points = int(line.split(':')[1].strip().split()[0])
        elif line.startswith('Flags:') and 'complex' in line.lower():
            is_complex = True
        elif re.match(r'^\d+\s+\S+', line):
            parts = line.split()
            if len(parts) >= 2:
                columns.append(parts[1])

    if not columns or not n_points:
        raise ValueError(f"Failed to parse LTspice header: {n_vars} vars, {n_points} points")

    # Parse binary data
    rows = []
    if is_complex:
        # Complex: first var is double (frequency), rest are pairs of doubles
        point_size = 8 + (n_vars - 1) * 16  # 8 bytes sweep + 16 bytes per complex var
        for i in range(n_points):
            offset = i * point_size
            chunk = data_bytes[offset:offset + point_size]
            if len(chunk) < point_size:
                break
            row = [struct.unpack_from('<d', chunk, 0)[0]]  # frequency
            for v in range(1, n_vars):
                re_val = struct.unpack_from('<d', chunk, 8 + (v-1)*16)[0]
                im_val = struct.unpack_from('<d', chunk, 8 + (v-1)*16 + 8)[0]
                row.append(re_val)  # just take real part for now
            rows.append(row)
    else:
        # Real: first var might be double, rest are floats (LTspice DC)
        # Actually LTspice uses 8-byte double for first, 4-byte float for rest
        point_size = 8 + (n_vars - 1) * 4
        for i in range(n_points):
            offset = i * point_size
            chunk = data_bytes[offset:offset + point_size]
            if len(chunk) < point_size:
                # Maybe all doubles
                break
            row = [struct.unpack_from('<d', chunk, 0)[0]]
            for v in range(1, n_vars):
                row.append(struct.unpack_from('<f', chunk, 8 + (v-1)*4)[0])
            rows.append(row)

        if not rows:
            # All doubles format (Xyce)
            point_size = n_vars * 8
            for i in range(n_points):
                offset = i * point_size
                chunk = data_bytes[offset:offset + point_size]
                if len(chunk) < point_size:
                    break
                row = list(struct.unpack_from(f'<{n_vars}d', chunk, 0))
                rows.append(row)

    return columns, rows


def _parse_raw_binary(f, header_bytes: bytes) -> Tuple[List[str], List[List[float]]]:
    """Parse standard binary .raw (Xyce/ngspice)."""
    header = header_bytes.decode('latin-1')
    n_vars = 0
    n_points = 0
    columns = []

    for line in header.split('\n'):
        line = line.strip()
        if line.startswith('No. Variables:'):
            n_vars = int(line.split(':')[1].strip())
        elif line.startswith('No. Points:'):
            n_points = int(line.split(':')[1].strip().split()[0])
        elif re.match(r'^\s*\d+\s+\S+', line):
            parts = line.split()
            if len(parts) >= 2:
                columns.append(parts[1])

    data = f.read()
    rows = []
    point_size = n_vars * 8
    for i in range(n_points):
        offset = i * point_size
        chunk = data[offset:offset + point_size]
        if len(chunk) < point_size:
            break
        row = list(struct.unpack_from(f'<{n_vars}d', chunk, 0))
        rows.append(row)

    return columns, rows


def _parse_raw_ascii(f, header_bytes: bytes) -> Tuple[List[str], List[List[float]]]:
    """Parse ASCII .raw after 'Values:' marker."""
    header = header_bytes.decode('latin-1')
    columns = []
    n_vars = 0

    for line in header.split('\n'):
        line = line.strip()
        if line.startswith('No. Variables:'):
            n_vars = int(line.split(':')[1].strip())
        elif re.match(r'^\s*\d+\s+\S+', line):
            parts = line.split()
            if len(parts) >= 2:
                columns.append(parts[1])

    rows = []
    current_row = []
    for line in f:
        line = line.decode('latin-1').strip()
        if not line:
            continue
        parts = line.split()
        for p in parts:
            try:
                current_row.append(float(p))
            except ValueError:
                continue
        if len(current_row) >= n_vars:
            rows.append(current_row[:n_vars])
            current_row = current_row[n_vars:]

    return columns, rows


def _parse_raw_ascii_full(path: str) -> Tuple[List[str], List[List[float]]]:
    """Fallback: parse entire file as ASCII .raw."""
    with open(path, 'r', errors='replace') as f:
        text = f.read()
    return _parse_raw_ascii(
        __import__('io').BytesIO(b''),
        text.encode('latin-1')
    )


# ── Sweep Generators ─────────────────────────────────────────────────

def gen_vdmos_sweeps(card: ModelCard) -> Dict[str, str]:
    """Generate characterization netlists for a VDMOS device."""
    vds_max = abs(card.params.get('vds', 30))
    vgs_max = min(abs(card.params.get('vto', 3)) * 3, 20)
    pchan = card.polarity == 'p'
    sign = -1 if pchan else 1

    model_line = card.raw_line

    sweeps = {}

    # IDS vs VDS (family of curves at different VGS)
    sweeps['ids_vds'] = f"""{card.name} VDMOS IDS vs VDS
M1 d g 0 0 {card.name}
Vgs g 0 0
Vds d 0 0
{model_line}
.dc Vds 0 {sign * vds_max} {sign * vds_max / 100} Vgs 0 {sign * vgs_max} {sign * vgs_max / 10}
.print DC I(Vds) V(d) V(g)
.end
"""

    # IDS vs VGS (transfer curve at fixed VDS)
    vds_fixed = sign * vds_max / 2
    sweeps['ids_vgs'] = f"""{card.name} VDMOS IDS vs VGS
M1 d g 0 0 {card.name}
Vgs g 0 0
Vds d 0 {vds_fixed}
{model_line}
.dc Vgs 0 {sign * vgs_max} {sign * vgs_max / 200}
.print DC I(Vgs) V(g) V(d)
.end
"""

    return sweeps


def gen_bjt_sweeps(card: ModelCard) -> Dict[str, str]:
    """Generate characterization netlists for a BJT."""
    pnp = card.polarity == 'p'
    sign = -1 if pnp else 1
    vce_max = 10
    ib_max = 100e-6

    model_line = card.raw_line

    sweeps = {}

    sweeps['ic_vce'] = f"""{card.name} BJT IC vs VCE
Q1 c b 0 0 {card.name}
Vce c 0 0
Ib 0 b 0
{model_line}
.dc Vce 0 {sign * vce_max} {sign * vce_max / 100} Ib 0 {sign * ib_max} {sign * ib_max / 10}
.print DC I(Vce) I(Ib) V(c) V(b)
.end
"""

    sweeps['ic_vbe'] = f"""{card.name} BJT IC vs VBE
Q1 c b 0 0 {card.name}
Vce c 0 {sign * vce_max / 2}
Vbe b 0 0
{model_line}
.dc Vbe 0 {sign * 0.8} {sign * 0.8 / 200}
.print DC I(Vce) V(b)
.end
"""

    return sweeps


def gen_diode_sweeps(card: ModelCard) -> Dict[str, str]:
    """Generate characterization netlists for a diode."""
    model_line = card.raw_line

    sweeps = {}

    sweeps['id_vd'] = f"""{card.name} Diode I-V
D1 a 0 {card.name}
Va a 0 0
{model_line}
.dc Va -5 1.2 0.01
.print DC I(Va) V(a)
.end
"""

    return sweeps


def gen_darlington_sweeps(card: ModelCard) -> Dict[str, str]:
    """Generate characterization netlists for a Darlington pair.

    Uses the BJT model card for both transistors.
    """
    model_line = card.raw_line
    # Darlington β ≈ β², so much lower Ib needed
    sweeps = {}

    sweeps['ic_vce'] = f"""{card.name} Darlington IC vs VCE
Q1 c b mid 0 {card.name}
Q2 c mid 0 0 {card.name}
Vce c 0 0
Ib 0 b 0
{model_line}
.dc Vce 0 5 0.05 Ib 1n 100n 10n
.print DC I(Vce) V(c) V(b) I(Ib)
.end
"""

    sweeps['ic_vbe'] = f"""{card.name} Darlington IC vs VBE
Q1 c b mid 0 {card.name}
Q2 c mid 0 0 {card.name}
Vce c 0 0
Vbe b 0 0
{model_line}
.dc Vbe 0 2.0 0.01 Vce 0.5 5 0.5
.print DC I(Vce) V(b) V(c)
.end
"""

    return sweeps


def gen_mirror_sweeps(card: ModelCard) -> Dict[str, str]:
    """Generate characterization netlists for a current mirror pair."""
    model_line = card.raw_line
    pnp = card.polarity == 'p'
    sign = -1 if pnp else 1

    sweeps = {}

    # Mirror ratio: Iout vs Iref at various Vce_out
    sweeps['iout_iref'] = f"""{card.name} Mirror Iout vs Iref
Q1 cref cref 0 0 {card.name}
Q2 cout cref 0 0 {card.name}
Vce cout 0 2.5
Iref 0 cref 0
{model_line}
.dc Iref 0 {sign * 1e-3} {sign * 10e-6} Vce {sign * 0.5} {sign * 5} {sign * 0.5}
.print DC I(Vce) I(Iref) V(cref) V(cout)
.end
"""

    return sweeps


SWEEP_GENERATORS = {
    'VDMOS': gen_vdmos_sweeps,
    'NPN': gen_bjt_sweeps,
    'PNP': gen_bjt_sweeps,
    'D': gen_diode_sweeps,
    'DARLINGTON': gen_darlington_sweeps,
    'MIRROR': gen_mirror_sweeps,
}


# ── Verilog-A Emitter ────────────────────────────────────────────────

def emit_vdmos_va(db: sqlite3.Connection, model_name: str) -> str:
    """Generate a Verilog-A VDMOS model from stored IV data.

    Uses an analytical VDMOS model fitted to the LTspice parameters
    (Vto, Kp, lambda, Rd, Rs, etc.) rather than a table lookup.
    This produces clean, compact VA that any compiler can handle.
    """
    row = db.execute(
        "SELECT params, polarity FROM models WHERE name=?",
        (model_name,)
    ).fetchone()
    if not row:
        raise ValueError(f"Model {model_name} not found in database")

    params = json.loads(row[0])
    polarity = row[1]
    pchan = polarity == 'p'

    # Extract VDMOS parameters (with defaults matching LTspice)
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
    m_diode = params.get('m', 0.5)
    rb = params.get('rb', 0.0)
    n_diode = params.get('n', 1.0)

    sign = "-" if pchan else ""
    neg = "" if pchan else "-"

    va = f"""`include "disciplines.vams"
`include "constants.vams"

// {model_name} — VDMOS {'PMOS' if pchan else 'NMOS'} analytical model
// Auto-generated by devchar from LTspice model parameters

module {model_name}(d, g, s);
    inout d, g, s;
    electrical d, g, s;
    electrical di, gi, si;  // internal nodes for Rd, Rg, Rs

    parameter real Vto      = {vto};
    parameter real Kp       = {kp};
    parameter real Lambda   = {lam};
    parameter real Rd_val   = {rd};
    parameter real Rs_val   = {rs};
    parameter real Rg_val   = {rg};
    parameter real Mtriode  = {mtriode};
    parameter real Ksubthres = {ksubthres};
    parameter real Cgs_val  = {cgs};
    parameter real Cgdmin   = {cgdmin};
    parameter real Cgdmax   = {cgdmax};
    parameter real A_cgd    = {a_cgd};
    parameter real Cjo_val  = {cjo};
    parameter real Is_val   = {is_diode};
    parameter real N_val    = {n_diode};

    real Vgs, Vds, Vgd, Ids, Vov, gm_sub;
    real Cgd_val;

    analog begin
        // Internal resistances
        if (Rd_val > 1e-9)
            V(d, di)  <+ I(d, di) * Rd_val;
        else
            V(d, di)  <+ 0.0;

        if (Rs_val > 1e-9)
            V(si, s)  <+ I(si, s) * Rs_val;
        else
            V(si, s)  <+ 0.0;

        if (Rg_val > 1e-9)
            V(g, gi)  <+ I(g, gi) * Rg_val;
        else
            V(g, gi)  <+ 0.0;

        // Terminal voltages (internal nodes)
        Vgs = {"V(si, gi)" if pchan else "V(gi, si)"};
        Vds = {"V(si, di)" if pchan else "V(di, si)"};
        Vgd = Vgs - Vds;
        Vov = Vgs - Vto;

        // MOSFET current: LTspice VDMOS equations
        if (Vov <= 0.0) begin
            // Subthreshold
            if (Ksubthres > 0.0)
                Ids = Kp * Ksubthres * Ksubthres *
                      ln(1.0 + exp(Vov / Ksubthres)) *
                      ln(1.0 + exp(Vov / Ksubthres));
            else
                Ids = 0.0;
        end
        else if (Vds < Vov) begin
            // Triode region
            Ids = Kp * (Vov * Vds - 0.5 * pow(Vds, Mtriode) *
                  pow(Vov, 2.0 - Mtriode)) * (1.0 + Lambda * Vds);
        end
        else begin
            // Saturation region
            Ids = 0.5 * Kp * Vov * Vov * (1.0 + Lambda * Vds);
        end

        // Stamp drain current
        I(di, si) <+ {sign}Ids;

        // Body diode (drain-source)
        I(si, di) <+ Is_val * (limexp(V(si, di) / (N_val * $vt)) - 1.0);

        // Capacitances
        I(gi, si) <+ Cgs_val * ddt(V(gi, si));

        // Nonlinear Cgd
        Cgd_val = Cgdmin + (Cgdmax - Cgdmin) / (1.0 + A_cgd * max(0.0, Vds));
        I(gi, di) <+ Cgd_val * ddt(V(gi, di));

        // Body diode junction capacitance
        I(di, si) <+ Cjo_val * ddt(V(di, si));
    end
endmodule
"""
    return va


def emit_bjt_va(db: sqlite3.Connection, model_name: str) -> str:
    """Generate a Verilog-A BJT model from stored parameters.

    Uses Gummel-Poon / Ebers-Moll equations matching SPICE level 1 BJT.
    """
    row = db.execute(
        "SELECT params, polarity FROM models WHERE name=?",
        (model_name,)
    ).fetchone()
    if not row:
        raise ValueError(f"Model {model_name} not found")

    params = json.loads(row[0])
    polarity = row[1]
    is_pnp = polarity == 'p'

    IS = params.get('is', 1e-16)
    BF = params.get('bf', 100.0)
    BR = params.get('br', 1.0)
    NF = params.get('nf', 1.0)
    NR = params.get('nr', 1.0)
    VAF = params.get('vaf', 1e10)
    VAR = params.get('var', 1e10)
    IKF = params.get('ikf', 1e10)
    RB = params.get('rb', 0.0)
    RC = params.get('rc', 0.0)
    RE = params.get('re', 0.0)
    CJE = params.get('cje', 0.0)
    CJC = params.get('cjc', 0.0)
    VJE = params.get('vje', 0.75)
    VJC = params.get('vjc', 0.75)
    MJE = params.get('mje', 0.33)
    MJC = params.get('mjc', 0.33)
    TF = params.get('tf', 0.0)
    TR = params.get('tr', 0.0)
    ISE = params.get('ise', 0.0)
    ISC = params.get('isc', 0.0)
    NE = params.get('ne', 1.5)
    NC = params.get('nc', 2.0)

    if is_pnp:
        vbe = "V(e, b)"
        vbc = "V(c, b)"
        vce = "V(e, c)"
        ice = "I(e, c)"
        ibe = "I(e, b)"
        ibc = "I(c, b)"
    else:
        vbe = "V(b, e)"
        vbc = "V(b, c)"
        vce = "V(c, e)"
        ice = "I(c, e)"
        ibe = "I(b, e)"
        ibc = "I(b, c)"

    return f"""`include "disciplines.vams"
`include "constants.vams"

// {model_name} — {'PNP' if is_pnp else 'NPN'} BJT (SPICE Gummel-Poon level 1)
// Auto-generated by devchar from LTspice characterization

module {model_name}(c, b, e);
    inout c, b, e;
    electrical c, b, e;

    parameter real IS  = {IS};
    parameter real BF  = {BF};
    parameter real BR  = {BR};
    parameter real NF  = {NF};
    parameter real NR  = {NR};
    parameter real VAF = {VAF};
    parameter real VAR = {VAR};
    parameter real IKF = {IKF};
    parameter real ISE = {ISE};
    parameter real ISC = {ISC};
    parameter real NE  = {NE};
    parameter real NC  = {NC};
    parameter real CJE = {CJE};
    parameter real CJC = {CJC};
    parameter real VJE = {VJE};
    parameter real VJC = {VJC};
    parameter real MJE = {MJE};
    parameter real MJC = {MJC};
    parameter real TF  = {TF};
    parameter real TR  = {TR};

    real Vbe, Vbc, If_fwd, Ir_rev, Ib_fwd, Ib_rev;
    real q1, q2, qb, Ic_total;
    real Cbe, Cbc, Qbe, Qbc;

    analog begin
        Vbe = {vbe};
        Vbc = {vbc};

        // Transport currents
        If_fwd = IS * (limexp(Vbe / (NF * $vt)) - 1.0);
        Ir_rev = IS * (limexp(Vbc / (NR * $vt)) - 1.0);

        // Base-emitter and base-collector recombination
        Ib_fwd = If_fwd / BF;
        Ib_rev = Ir_rev / BR;
        if (ISE > 0.0)
            Ib_fwd = Ib_fwd + ISE * (limexp(Vbe / (NE * $vt)) - 1.0);
        if (ISC > 0.0)
            Ib_rev = Ib_rev + ISC * (limexp(Vbc / (NC * $vt)) - 1.0);

        // Gummel-Poon base charge factor
        q1 = 1.0 / (1.0 - Vbc / VAF - Vbe / VAR);
        if (q1 < 0.1) q1 = 0.1;
        q2 = If_fwd / IKF;
        qb = q1 * (1.0 + sqrt(1.0 + 4.0 * q2)) / 2.0;

        // Collector current
        Ic_total = (If_fwd - Ir_rev) / qb;

        // Stamp terminal currents
        {ice} <+ Ic_total;
        {ibe} <+ Ib_fwd;
        {ibc} <+ Ib_rev;

        // Junction capacitances (depletion)
        if (Vbe < 0.5 * VJE)
            Cbe = CJE * pow(1.0 - Vbe / VJE, -MJE);
        else
            Cbe = CJE * pow(0.5, -MJE) * (1.0 + MJE * (Vbe - 0.5 * VJE) / VJE);

        if (Vbc < 0.5 * VJC)
            Cbc = CJC * pow(1.0 - Vbc / VJC, -MJC);
        else
            Cbc = CJC * pow(0.5, -MJC) * (1.0 + MJC * (Vbc - 0.5 * VJC) / VJC);

        // Diffusion charge
        Qbe = TF * If_fwd + Cbe * Vbe;
        Qbc = TR * Ir_rev + Cbc * Vbc;

        {ibe} <+ ddt(Qbe);
        {ibc} <+ ddt(Qbc);
    end
endmodule
"""


VA_EMITTERS = {
    'VDMOS': emit_vdmos_va,
    'NPN': emit_bjt_va,
    'PNP': emit_bjt_va,
}


# ── Native .so Emitter (PyMS ABI) ────────────────────────────────────

def emit_vdmos_so_source(db: sqlite3.Connection, model_name: str) -> str:
    """Generate C++ source for a VDMOS .so plugin (PyMS/Xyce ABI).

    The .so exports vae_eval and vae_jacobian, stamping:
      F[0] = Ids into drain node (KCL)
      F[1] = 0 (gate, no DC current)
      F[2] = -Ids into source node (KCL)
    Jacobian: dIds/dVgs, dIds/dVds via finite differences on the table.

    VaeState.V[0] = V(drain), V[1] = V(gate), V[2] = V(source)
    """
    row = db.execute(
        "SELECT params, polarity FROM models WHERE name=?",
        (model_name,)
    ).fetchone()
    if not row:
        raise ValueError(f"Model {model_name} not found")

    params = json.loads(row[0])
    polarity = row[1]
    pchan = polarity == 'p'

    sweep = db.execute(
        "SELECT columns, data, n_points FROM sweeps "
        "WHERE model=? AND sweep_type='ids_vds'",
        (model_name,)
    ).fetchone()
    if not sweep:
        raise ValueError(f"No ids_vds sweep data for {model_name}")

    columns = json.loads(sweep[0])
    data = list(struct.iter_unpack(f'<{len(columns)}d', sweep[1]))

    # Build table from LTspice data
    # LTspice columns: [vds_sweep, V(d), V(g), I(Vds)]
    # I(Vds) is current INTO Vds source = -Ids
    vgs_set = sorted(set(abs(r[2]) for r in data))
    vds_set = sorted(set(abs(r[1]) for r in data))

    ids_table = {}
    for r in data:
        vds = abs(r[1])
        vgs = abs(r[2])
        ids = abs(r[3])  # column index 3 = I(Vds), take abs
        ids_table[(vgs, vds)] = ids

    # Subsample
    max_vgs, max_vds = 32, 128
    if len(vgs_set) > max_vgs:
        step = max(1, len(vgs_set) // max_vgs)
        vgs_set = vgs_set[::step]
    if len(vds_set) > max_vds:
        step = max(1, len(vds_set) // max_vds)
        vds_set = vds_set[::step]

    n_vgs = len(vgs_set)
    n_vds = len(vds_set)

    def get_ids(vgs, vds):
        return ids_table.get((vgs, vds), 0.0)

    cgs = params.get('cgs', 0)
    cgd = params.get('cgdmin', 0)
    cds = params.get('cjo', 0)

    # Generate C++ source
    cpp = f"""// {model_name} — VDMOS {'PMOS' if pchan else 'NMOS'} table model
// Auto-generated by devchar — PyMS/Xyce ABI
// {n_vgs} x {n_vds} IDS(VGS,VDS) bilinear interpolation table
//
// Nodes: V[0]=drain, V[1]=gate, V[2]=source
// F[0]=+Ids (drain), F[1]=0 (gate), F[2]=-Ids (source)
// Q for charge-based capacitances

#include <cstring>
#include <cmath>

struct VaeState {{ double V[16]; double Vt; }};

static const int N_VGS = {n_vgs};
static const int N_VDS = {n_vds};

static const double vgs_bp[{n_vgs}] = {{
    {', '.join(f'{v:.8e}' for v in vgs_set)}
}};

static const double vds_bp[{n_vds}] = {{
    {', '.join(f'{v:.8e}' for v in vds_set)}
}};

static const double ids_tbl[{n_vgs}][{n_vds}] = {{
"""
    for i, vgs in enumerate(vgs_set):
        row_vals = [f'{get_ids(vgs, vds):.8e}' for vds in vds_set]
        cpp += f"    {{ {', '.join(row_vals)} }},\n"

    sign = -1 if pchan else 1
    cpp += f"""
}};

static const double CGS = {cgs:.6e};
static const double CGD = {cgd:.6e};
static const double CDS = {cds:.6e};

static inline double interp2d(double x, double y,
    const double* xbp, int nx, const double* ybp, int ny,
    const double* tbl)
{{
    // Find x index
    int ix = 0;
    if (x <= xbp[0]) ix = 0;
    else if (x >= xbp[nx-1]) ix = nx - 2;
    else {{ while (ix < nx-2 && xbp[ix+1] < x) ix++; }}

    // Find y index
    int iy = 0;
    if (y <= ybp[0]) iy = 0;
    else if (y >= ybp[ny-1]) iy = ny - 2;
    else {{ while (iy < ny-2 && ybp[iy+1] < y) iy++; }}

    double fx = (x - xbp[ix]) / (xbp[ix+1] - xbp[ix] + 1e-30);
    double fy = (y - ybp[iy]) / (ybp[iy+1] - ybp[iy] + 1e-30);
    if (fx < 0) fx = 0; if (fx > 1) fx = 1;
    if (fy < 0) fy = 0; if (fy > 1) fy = 1;

    double v00 = tbl[ix * ny + iy];
    double v01 = tbl[ix * ny + iy + 1];
    double v10 = tbl[(ix+1) * ny + iy];
    double v11 = tbl[(ix+1) * ny + iy + 1];

    return v00*(1-fx)*(1-fy) + v10*fx*(1-fy) + v01*(1-fx)*fy + v11*fx*fy;
}}

extern "C" {{

int vae_n_nodes() {{ return 3; }}   // drain, gate, source
int vae_n_branches() {{ return 3; }}

void vae_eval(VaeState* s, double* F, double* Q)
{{
    double Vd = s->V[0], Vg = s->V[1], Vs = s->V[2];
    double Vgs = {'Vs - Vg' if pchan else 'Vg - Vs'};
    double Vds = {'Vs - Vd' if pchan else 'Vd - Vs'};

    // Clamp to table range
    if (Vgs < 0) Vgs = 0;
    if (Vds < 0) Vds = 0;

    double Ids = interp2d(Vgs, Vds, vgs_bp, N_VGS, vds_bp, N_VDS,
                          &ids_tbl[0][0]);

    // Stamp KCL: current out of drain = +Ids, into source = -Ids
    F[0] = {'' if not pchan else '-'}Ids;    // drain
    F[1] = 0;                                 // gate (no DC current)
    F[2] = {'-' if not pchan else ''}Ids;    // source

    // Charge for capacitances: Q = C * V
    Q[0] = CDS * (Vd - Vs) + CGD * (Vd - Vg);  // drain charge
    Q[1] = CGS * (Vg - Vs) + CGD * (Vg - Vd);  // gate charge
    Q[2] = -(CGS * (Vg - Vs) + CDS * (Vd - Vs)); // source charge
}}

void vae_jacobian(VaeState* s, double* dFdV, double* dQdV)
{{
    // Finite-difference Jacobian
    // dFdV[i*n + j] = dF[i]/dV[j], n=3 nodes
    double Vd = s->V[0], Vg = s->V[1], Vs = s->V[2];
    const double dv = 1e-6;

    // Evaluate at perturbed points
    VaeState sp = *s;
    double F0[3], Q0[3], Fp[3], Qp[3];

    memset(dFdV, 0, 3*3*sizeof(double));
    memset(dQdV, 0, 3*3*sizeof(double));

    vae_eval(s, F0, Q0);

    for (int j = 0; j < 3; j++) {{
        sp = *s;
        sp.V[j] += dv;
        vae_eval(&sp, Fp, Qp);
        for (int i = 0; i < 3; i++) {{
            dFdV[i*3 + j] = (Fp[i] - F0[i]) / dv;
            dQdV[i*3 + j] = (Qp[i] - Q0[i]) / dv;
        }}
    }}
}}

}} // extern "C"
"""
    return cpp


def compile_so(cpp_source: str, output_path: str) -> bool:
    """Compile C++ source to shared library."""
    with tempfile.NamedTemporaryFile(suffix='.cpp', mode='w', delete=False) as f:
        f.write(cpp_source)
        cpp_path = f.name

    try:
        result = subprocess.run(
            ['g++', '-shared', '-fPIC', '-O2', '-o', output_path, cpp_path],
            capture_output=True, timeout=30
        )
        if result.returncode != 0:
            print(f"Compilation failed:\n{result.stderr.decode()}", file=sys.stderr)
            return False
        return True
    finally:
        os.unlink(cpp_path)


def emit_bjt_so_source(db: sqlite3.Connection, model_name: str) -> str:
    """Generate C++ source for a BJT .so plugin (PyMS/Xyce ABI)."""
    row = db.execute(
        "SELECT params, polarity FROM models WHERE name=?",
        (model_name,)
    ).fetchone()
    if not row:
        raise ValueError(f"Model {model_name} not found")

    params = json.loads(row[0])
    polarity = row[1]
    is_pnp = polarity == 'p'

    IS = params.get('is', 1e-16)
    BF = params.get('bf', 100.0)
    BR = params.get('br', 1.0)
    NF = params.get('nf', 1.0)
    NR = params.get('nr', 1.0)
    VAF = params.get('vaf', 1e10)
    CJE = params.get('cje', 0.0)
    CJC = params.get('cjc', 0.0)
    RB = params.get('rb', 0.0)

    # Nodes: V[0]=c, V[1]=b, V[2]=e
    if is_pnp:
        vbe = "Vs->V[2] - Vs->V[1]"  # V(e,b)
        vbc = "Vs->V[0] - Vs->V[1]"  # V(c,b)
        vce = "Vs->V[2] - Vs->V[0]"  # V(e,c)
    else:
        vbe = "Vs->V[1] - Vs->V[2]"  # V(b,e)
        vbc = "Vs->V[1] - Vs->V[0]"  # V(b,c)
        vce = "Vs->V[0] - Vs->V[2]"  # V(c,e)

    sign = -1 if is_pnp else 1

    return f"""// {model_name} — {'PNP' if is_pnp else 'NPN'} BJT (Gummel-Poon)
// Auto-generated by devchar — PyMS/Xyce ABI
// Nodes: V[0]=c, V[1]=b, V[2]=e

#include <cstring>
#include <cmath>

struct VaeState {{ double V[16]; double Vt; }};

static const double IS  = {IS};
static const double BF  = {BF};
static const double BR  = {BR};
static const double NF  = {NF};
static const double NR  = {NR};
static const double VAF = {VAF};
static const double CJE = {CJE};
static const double CJC = {CJC};

static inline double limexp(double x) {{
    return (x < 80.0) ? exp(x) : exp(80.0) * (1.0 + x - 80.0);
}}

extern "C" {{

int vae_n_nodes() {{ return 3; }}
int vae_n_branches() {{ return 3; }}

void vae_eval(VaeState* Vs, double* F, double* Q)
{{
    double Vbe = {vbe};
    double Vbc = {vbc};
    double Vce = {vce};
    double vt = Vs->Vt;

    // Transport currents
    double If = IS * (limexp(Vbe / (NF * vt)) - 1.0);
    double Ir = IS * (limexp(Vbc / (NR * vt)) - 1.0);

    // Gummel-Poon base charge
    double q1 = 1.0 / (1.0 - Vbc / VAF);
    if (q1 < 0.1) q1 = 0.1;
    double Ic = (If - Ir) / q1;

    // Base currents
    double Ibe = If / BF;
    double Ibc = Ir / BR;

    // Stamp KCL: F[0]=c, F[1]=b, F[2]=e
    // I(c,e) = Ic, I(b,e) = Ibe, I(b,c) = Ibc
    F[0] = {sign} * (Ic - Ibc);      // collector: +Ic - Ibc (for NPN)
    F[1] = {sign} * (Ibe + Ibc);     // base: +Ibe + Ibc
    F[2] = {sign} * (-Ic - Ibe);     // emitter: -Ic - Ibe (KCL)

    // Charge
    Q[0] = {sign} * (-CJC * Vbc);    // collector charge
    Q[1] = {sign} * (CJE * Vbe + CJC * Vbc);  // base charge
    Q[2] = {sign} * (-CJE * Vbe);    // emitter charge
}}

void vae_jacobian(VaeState* Vs, double* dFdV, double* dQdV)
{{
    // Finite-difference Jacobian
    double dv = 1e-6;
    double F0[3], Q0[3], Fp[3], Qp[3];
    VaeState sp;

    memset(dFdV, 0, 9*sizeof(double));
    memset(dQdV, 0, 9*sizeof(double));

    vae_eval(Vs, F0, Q0);

    for (int j = 0; j < 3; j++) {{
        sp = *Vs;
        sp.V[j] += dv;
        vae_eval(&sp, Fp, Qp);
        for (int i = 0; i < 3; i++) {{
            dFdV[i*3 + j] = (Fp[i] - F0[i]) / dv;
            dQdV[i*3 + j] = (Qp[i] - Q0[i]) / dv;
        }}
    }}
}}

}} // extern "C"
"""


def emit_darlington_so_source(db: sqlite3.Connection, model_name: str) -> str:
    """Generate C++ for a Darlington table-model .so.

    Uses IC vs VCE sweep data as a 2D lookup table.
    Nodes: V[0]=c, V[1]=b, V[2]=e (3 terminals, no internal nodes).
    """
    row = db.execute(
        "SELECT params, polarity FROM models WHERE name=?",
        (model_name,)
    ).fetchone()
    if not row:
        raise ValueError(f"Model {model_name} not found")

    params = json.loads(row[0])

    sweep = db.execute(
        "SELECT columns, data, n_points FROM sweeps "
        "WHERE model=? AND sweep_type='ic_vce'",
        (model_name,)
    ).fetchone()
    if not sweep:
        raise ValueError(f"No ic_vce sweep data for {model_name}")

    columns = json.loads(sweep[0])
    data = list(struct.iter_unpack(f'<{len(columns)}d', sweep[1]))

    # Build table: Ic(Ib, Vce)
    # Columns: I(Vce), V(c), V(b), I(Ib)
    ib_vals = sorted(set(abs(r[3]) for r in data if len(r) > 3))
    vce_vals = sorted(set(abs(r[1]) for r in data))

    ic_table = {}
    for r in data:
        if len(r) < 4:
            continue
        ic = abs(r[0])
        vce = abs(r[1])
        ib = abs(r[3])
        ic_table[(ib, vce)] = ic

    # Subsample
    max_ib, max_vce = 16, 64
    if len(ib_vals) > max_ib:
        step = max(1, len(ib_vals) // max_ib)
        ib_vals = ib_vals[::step]
    if len(vce_vals) > max_vce:
        step = max(1, len(vce_vals) // max_vce)
        vce_vals = vce_vals[::step]

    n_ib = len(ib_vals)
    n_vce = len(vce_vals)

    def get_ic(ib, vce):
        return ic_table.get((ib, vce), 0.0)

    return f"""// {model_name} — Darlington table model
// {n_ib} x {n_vce} Ic(Ib, Vce) bilinear interpolation
// Nodes: V[0]=c, V[1]=b, V[2]=e

#include <cstring>
#include <cmath>

struct VaeState {{ double V[16]; double Vt; }};

static const int N_IB = {n_ib};
static const int N_VCE = {n_vce};

static const double ib_bp[{n_ib}] = {{
    {', '.join(f'{v:.8e}' for v in ib_vals)}
}};

static const double vce_bp[{n_vce}] = {{
    {', '.join(f'{v:.8e}' for v in vce_vals)}
}};

static const double ic_tbl[{n_ib}][{n_vce}] = {{
{chr(10).join('    { ' + ', '.join(f'{get_ic(ib,vce):.8e}' for vce in vce_vals) + ' },' for ib in ib_vals)}
}};

static double interp2d(double x, double y,
    const double* xbp, int nx, const double* ybp, int ny, const double* tbl)
{{
    int ix = 0;
    if (x <= xbp[0]) ix = 0;
    else if (x >= xbp[nx-1]) ix = nx - 2;
    else {{ while (ix < nx-2 && xbp[ix+1] < x) ix++; }}
    int iy = 0;
    if (y <= ybp[0]) iy = 0;
    else if (y >= ybp[ny-1]) iy = ny - 2;
    else {{ while (iy < ny-2 && ybp[iy+1] < y) iy++; }}
    double fx = (x - xbp[ix]) / (xbp[ix+1] - xbp[ix] + 1e-30);
    double fy = (y - ybp[iy]) / (ybp[iy+1] - ybp[iy] + 1e-30);
    if (fx < 0) fx = 0; if (fx > 1) fx = 1;
    if (fy < 0) fy = 0; if (fy > 1) fy = 1;
    double v00 = tbl[ix*ny+iy], v01 = tbl[ix*ny+iy+1];
    double v10 = tbl[(ix+1)*ny+iy], v11 = tbl[(ix+1)*ny+iy+1];
    return v00*(1-fx)*(1-fy) + v10*fx*(1-fy) + v01*(1-fx)*fy + v11*fx*fy;
}}

extern "C" {{

int vae_n_nodes() {{ return 3; }}
int vae_n_branches() {{ return 3; }}

void vae_eval(VaeState* s, double* F, double* Q)
{{
    double Vc = s->V[0], Vb = s->V[1], Ve = s->V[2];
    double Vbe = Vb - Ve;
    double Vce = Vc - Ve;
    double vt = s->Vt;

    // Estimate Ib from Vbe using exponential approximation
    // Ib ≈ IS/BF * exp(Vbe/Vt) for Darlington
    double IS = {params.get('is', 1e-16)};
    double BF = {params.get('bf', 125.0)};
    double Ib = IS/BF * (exp(fmin(Vbe/vt, 80.0)) - 1.0);
    if (Ib < 0) Ib = 0;

    double Ic = interp2d(Ib, fabs(Vce), ib_bp, N_IB, vce_bp, N_VCE, &ic_tbl[0][0]);

    F[0] = Ic;       // collector
    F[1] = Ib;       // base
    F[2] = -Ic - Ib; // emitter (KCL)

    memset(Q, 0, 3*sizeof(double));
}}

void vae_jacobian(VaeState* s, double* dFdV, double* dQdV)
{{
    double dv = 1e-6;
    double F0[3], Q0[3], Fp[3], Qp[3];
    VaeState sp;
    memset(dFdV, 0, 9*sizeof(double));
    memset(dQdV, 0, 9*sizeof(double));
    vae_eval(s, F0, Q0);
    for (int j = 0; j < 3; j++) {{
        sp = *s; sp.V[j] += dv;
        vae_eval(&sp, Fp, Qp);
        for (int i = 0; i < 3; i++) {{
            dFdV[i*3+j] = (Fp[i] - F0[i]) / dv;
            dQdV[i*3+j] = (Qp[i] - Q0[i]) / dv;
        }}
    }}
}}

}} // extern "C"
"""


SO_EMITTERS = {
    'VDMOS': emit_vdmos_so_source,
    'NPN': emit_bjt_so_source,
    'PNP': emit_bjt_so_source,
    'DARLINGTON': emit_darlington_so_source,
}


# ── CLI Commands ──────────────────────────────────────────────────────

def cmd_extract(args):
    """Extract IV curves for a single model."""
    db = get_db()
    runner = RUNNERS[args.sim]()

    # Parse model card
    if args.model_line:
        cards = ModelCard.parse_spice_lib(args.model_line)
    elif args.lib_file:
        text = Path(args.lib_file).read_text(errors='replace')
        cards = ModelCard.parse_spice_lib(text)
        cards = [c for c in cards if c.name == args.model]
    else:
        print(f"Need --model-line or --lib-file", file=sys.stderr)
        return 1

    if not cards:
        print(f"Model {args.model} not found", file=sys.stderr)
        return 1

    card = cards[0]
    print(f"Extracting {card.name} ({card.dev_type}, {card.polarity}-channel)")

    # Get sweep generator
    gen = SWEEP_GENERATORS.get(card.dev_type)
    if not gen:
        print(f"No sweep generator for {card.dev_type}", file=sys.stderr)
        return 1

    sweeps = gen(card)

    # Store model
    db.execute("""
        INSERT OR REPLACE INTO models (name, dev_type, polarity, params,
            source_sim, extracted, notes)
        VALUES (?, ?, ?, ?, ?, datetime('now'), ?)
    """, (card.name, card.dev_type, card.polarity,
          json.dumps(card.params), runner.name(), card.raw_line))

    # Run each sweep
    for sweep_name, netlist in sweeps.items():
        print(f"  Running {sweep_name}...", end=' ', flush=True)

        with tempfile.TemporaryDirectory(prefix='devchar_') as workdir:
            raw_path = runner.run(netlist, workdir)
            if not raw_path:
                print("FAILED (no output)")
                continue

            try:
                columns, rows = parse_raw(raw_path)
            except Exception as e:
                print(f"FAILED (parse: {e})")
                continue

            # Pack data as float64
            packed = b''
            for row in rows:
                packed += struct.pack(f'<{len(columns)}d', *row[:len(columns)])

            db.execute("""
                INSERT OR REPLACE INTO sweeps
                    (model, sweep_type, conditions, n_points, data, columns)
                VALUES (?, ?, '{}', ?, ?, ?)
            """, (card.name, sweep_name, len(rows), packed,
                  json.dumps(columns)))

            print(f"OK ({len(rows)} points, {len(columns)} columns)")

    db.commit()
    print(f"\nStored in {DB_PATH}")
    return 0


def cmd_extract_lib(args):
    """Extract all models of a given type from a library file."""
    db = get_db()
    runner = RUNNERS[args.sim]()

    text = Path(args.lib_file).read_text(errors='replace')
    text = re.sub(r'\n\+', ' ', text)  # join continuation lines
    cards = ModelCard.parse_spice_lib(text)

    if args.type:
        cards = [c for c in cards if c.dev_type.upper() == args.type.upper()]

    print(f"Found {len(cards)} {args.type or 'all'} models in {args.lib_file}")

    if args.limit:
        cards = cards[:args.limit]
        print(f"  (limited to first {args.limit})")

    for card in cards:
        gen = SWEEP_GENERATORS.get(card.dev_type)
        if not gen:
            continue

        sweeps = gen(card)
        print(f"\n{card.name} ({card.dev_type}, {card.polarity}-ch)", flush=True)

        db.execute("""
            INSERT OR REPLACE INTO models (name, dev_type, polarity, params,
                source_sim, extracted, notes)
            VALUES (?, ?, ?, ?, ?, datetime('now'), ?)
        """, (card.name, card.dev_type, card.polarity,
              json.dumps(card.params), runner.name(), card.raw_line))

        for sweep_name, netlist in sweeps.items():
            print(f"  {sweep_name}...", end=' ', flush=True)
            with tempfile.TemporaryDirectory(prefix='devchar_') as workdir:
                raw_path = runner.run(netlist, workdir)
                if not raw_path:
                    print("FAIL")
                    continue
                try:
                    columns, rows = parse_raw(raw_path)
                except Exception as e:
                    print(f"FAIL ({e})")
                    continue

                packed = b''
                for row in rows:
                    packed += struct.pack(f'<{len(columns)}d', *row[:len(columns)])

                db.execute("""
                    INSERT OR REPLACE INTO sweeps
                        (model, sweep_type, conditions, n_points, data, columns)
                    VALUES (?, ?, '{}', ?, ?, ?)
                """, (card.name, sweep_name, len(rows), packed,
                      json.dumps(columns)))
                print(f"OK ({len(rows)} pts)")

        db.commit()

    print(f"\nDatabase: {DB_PATH}")
    return 0


def cmd_list(args):
    """List models in the database."""
    db = get_db()
    rows = db.execute("""
        SELECT m.name, m.dev_type, m.polarity, m.source_sim, m.extracted,
               COUNT(s.id), SUM(s.n_points)
        FROM models m LEFT JOIN sweeps s ON m.name = s.model
        GROUP BY m.name
        ORDER BY m.dev_type, m.name
    """).fetchall()

    if not rows:
        print("(empty database)")
        return

    print(f"{'Model':<25} {'Type':<8} {'Pol':<4} {'Sim':<10} {'Sweeps':<8} {'Points':<10} {'Date'}")
    print("-" * 90)
    for name, dtype, pol, sim, date, n_sweeps, n_pts in rows:
        print(f"{name:<25} {dtype:<8} {pol:<4} {sim:<10} {n_sweeps:<8} {n_pts or 0:<10} {date or ''}")


def cmd_emit_va(args):
    """Generate Verilog-A from database."""
    db = get_db()
    row = db.execute("SELECT dev_type FROM models WHERE name=?",
                     (args.model,)).fetchone()
    if not row:
        print(f"Model {args.model} not found", file=sys.stderr)
        return 1

    emitter = VA_EMITTERS.get(row[0])
    if not emitter:
        print(f"No Verilog-A emitter for {row[0]}", file=sys.stderr)
        return 1

    va = emitter(db, args.model)

    if args.output:
        Path(args.output).write_text(va)
        print(f"Wrote {args.output}")
    else:
        print(va)


def cmd_compile_so(args):
    """Compile a model to .so (PyMS/Xyce ABI)."""
    db = get_db()
    row = db.execute("SELECT dev_type FROM models WHERE name=?",
                     (args.model,)).fetchone()
    if not row:
        print(f"Model {args.model} not found", file=sys.stderr)
        return 1

    emitter = SO_EMITTERS.get(row[0])
    if not emitter:
        print(f"No .so emitter for {row[0]}", file=sys.stderr)
        return 1

    cpp = emitter(db, args.model)

    output = args.output or f"{args.model}.so"
    if compile_so(cpp, output):
        print(f"Compiled {output}")
        # Verify exports
        result = subprocess.run(['nm', '-D', output], capture_output=True)
        exports = result.stdout.decode()
        for sym in ('vae_eval', 'vae_jacobian', 'vae_n_nodes'):
            if sym in exports:
                print(f"  ✓ {sym}")
            else:
                print(f"  ✗ {sym} MISSING")
    else:
        return 1


def cmd_dump(args):
    """Dump sweep data as CSV."""
    db = get_db()
    sweep = db.execute(
        "SELECT columns, data, n_points FROM sweeps "
        "WHERE model=? AND sweep_type=?",
        (args.model, args.sweep)
    ).fetchone()

    if not sweep:
        print(f"No {args.sweep} data for {args.model}", file=sys.stderr)
        return 1

    columns = json.loads(sweep[0])
    n_cols = len(columns)
    data = sweep[1]

    print(",".join(columns))
    for vals in struct.iter_unpack(f'<{n_cols}d', data):
        print(",".join(f"{v:.6e}" for v in vals))


def main():
    p = argparse.ArgumentParser(
        description='Device characterization database builder'
    )
    sub = p.add_subparsers(dest='command')

    # extract
    ex = sub.add_parser('extract', help='Extract a single model')
    ex.add_argument('--model', required=True)
    ex.add_argument('--model-line', help='Raw .model line')
    ex.add_argument('--lib-file', help='Library file containing model')
    ex.add_argument('--type', help='Device type override')
    ex.add_argument('--sim', default='ltspice', choices=RUNNERS.keys())

    # extract-lib
    el = sub.add_parser('extract-lib', help='Extract all models from library')
    el.add_argument('lib_file')
    el.add_argument('--type', help='Filter by device type')
    el.add_argument('--sim', default='ltspice', choices=RUNNERS.keys())
    el.add_argument('--limit', type=int, help='Max models to extract')

    # list
    sub.add_parser('list', help='List models in database')

    # emit-va
    ev = sub.add_parser('emit-va', help='Generate Verilog-A')
    ev.add_argument('--model', required=True)
    ev.add_argument('-o', '--output', help='Output .va file')

    # compile-so
    cs = sub.add_parser('compile-so', help='Compile model to .so plugin')
    cs.add_argument('--model', required=True)
    cs.add_argument('-o', '--output', help='Output .so path')

    # dump
    dm = sub.add_parser('dump', help='Dump sweep as CSV')
    dm.add_argument('--model', required=True)
    dm.add_argument('--sweep', required=True)

    args = p.parse_args()
    if not args.command:
        p.print_help()
        return

    cmd = {
        'extract': cmd_extract,
        'extract-lib': cmd_extract_lib,
        'list': cmd_list,
        'emit-va': cmd_emit_va,
        'compile-so': cmd_compile_so,
        'dump': cmd_dump,
    }[args.command]
    sys.exit(cmd(args) or 0)


if __name__ == '__main__':
    main()
