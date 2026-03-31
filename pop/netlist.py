"""
ltz/pop/netlist.py

Xyce netlist manipulation for POP analysis:
  - parse_state_vars()     scan netlist for C/L state variables
  - inject_ics()           write a run copy with .IC directives replaced
  - write_tran_directive() set .TRAN stop time and timestep
  - read_pop_annotations() extract POP_TRIGGER / POP_PERIOD from comments
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import NamedTuple

import numpy as np


# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------

class StateVar(NamedTuple):
    """One state variable extracted from the netlist."""
    kind: str   # 'V' (capacitor node) or 'I' (inductor)
    name: str   # node name for V, element name for I
    ic: float   # value found in existing .IC line, 0.0 if absent


class POPAnnotations(NamedTuple):
    trigger_node: str | None
    period_sec: float | None


# ---------------------------------------------------------------------------
# Regex patterns
# ---------------------------------------------------------------------------

# .IC V(node)=val  or  .IC I(Lname)=val  (case-insensitive, optional spaces)
_IC_RE = re.compile(
    r"^\s*\.IC\s+"
    r"(?P<kind>[VI])\((?P<name>[^\)]+)\)"
    r"\s*=\s*(?P<val>[+-]?(?:\d+\.?\d*|\.\d+)(?:[eE][+-]?\d+)?)",
    re.IGNORECASE,
)

# Passive element lines:  Cxxx n+ n- val [IC=...]
#                         Lxxx n+ n- val [IC=...]
_CAP_RE = re.compile(r"^\s*C\S+\s+(\S+)\s+(\S+)\s+", re.IGNORECASE)
_IND_RE = re.compile(r"^\s*(L\S+)\s+\S+\s+\S+\s+", re.IGNORECASE)

# .TRAN directive
_TRAN_RE = re.compile(r"^\s*\.TRAN\b.*", re.IGNORECASE)

# ltz POP annotations in comments
_ANN_TRIGGER_RE = re.compile(r"^\s*\*\s*POP_TRIGGER\s*:\s*(\S+)", re.IGNORECASE)
_ANN_PERIOD_RE  = re.compile(r"^\s*\*\s*POP_PERIOD\s*:\s*(\S+)",  re.IGNORECASE)

# IC block sentinel comment written by ltz
_IC_BLOCK_START = "* ltz-pop IC block -- auto-generated, do not edit below"
_IC_BLOCK_END   = "* ltz-pop IC block end"


# ---------------------------------------------------------------------------
# Period / frequency string parser
# ---------------------------------------------------------------------------

_SUFFIX = {
    "fs": 1e-15, "ps": 1e-12, "ns": 1e-9, "us": 1e-6,
    "ms": 1e-3,  "s": 1.0,
    "ghz": 1e9, "mhz": 1e6, "khz": 1e3, "hz": 1.0,
}

def parse_period(s: str) -> float:
    """
    Parse a period or frequency string to seconds.

    Examples: '2us', '500kHz', '1e-6', '2e6Hz'
    Frequency inputs are converted: period = 1/f.
    """
    s = s.strip()
    m = re.match(
        r"(?P<val>[+-]?(?:\d+\.?\d*|\.\d+)(?:[eE][+-]?\d+)?)"
        r"\s*(?P<unit>[a-zA-Z]*)",
        s,
    )
    if not m:
        raise ValueError(f"Cannot parse period/frequency: {s!r}")
    val  = float(m.group("val"))
    unit = m.group("unit").lower()

    if not unit:
        return val                          # bare number → seconds

    if unit not in _SUFFIX:
        raise ValueError(f"Unknown unit {unit!r} in {s!r}")

    scaled = val * _SUFFIX[unit]

    if unit.endswith("hz"):                 # it was a frequency
        if scaled == 0:
            raise ValueError("Zero frequency")
        return 1.0 / scaled

    return scaled                           # it was already a period


# ---------------------------------------------------------------------------
# Annotation reader
# ---------------------------------------------------------------------------

def read_pop_annotations(netlist: Path) -> POPAnnotations:
    """
    Scan a netlist for ltz POP comment annotations:

        * POP_TRIGGER: SW
        * POP_PERIOD:  2us      (or e.g. 500kHz)

    Returns POPAnnotations with None fields for anything not found.
    """
    trigger = None
    period  = None
    for line in netlist.read_text().splitlines():
        if trigger is None:
            m = _ANN_TRIGGER_RE.match(line)
            if m:
                trigger = m.group(1)
        if period is None:
            m = _ANN_PERIOD_RE.match(line)
            if m:
                period = parse_period(m.group(1))
        if trigger and period:
            break
    return POPAnnotations(trigger, period)


# ---------------------------------------------------------------------------
# State variable discovery
# ---------------------------------------------------------------------------

def parse_state_vars(netlist: Path) -> list[StateVar]:
    """
    Return ordered list of StateVar for all capacitor nodes and inductors.

    Strategy:
      1. Collect explicit .IC lines (have user-supplied initial values).
      2. Scan C/L element lines for any state variables not already listed.
      3. Merge, preserving order: .IC entries first, then discovered elements.

    Capacitor positive node → V(node)
    Inductor element name  → I(Lname)
    """
    lines = netlist.read_text().splitlines()
    # skip ltz-generated IC block (stale from a previous run)
    lines = _strip_ic_block(lines)

    ic_map: dict[tuple[str, str], float] = {}   # (kind, name) → val

    # Pass 1: explicit .IC directives
    for line in lines:
        m = _IC_RE.match(line)
        if m:
            key = (m.group("kind").upper(), m.group("name"))
            ic_map[key] = float(m.group("val"))

    # Pass 2: element lines not yet covered
    for line in lines:
        cm = _CAP_RE.match(line)
        if cm:
            node = cm.group(1)              # positive terminal
            key = ("V", node)
            ic_map.setdefault(key, 0.0)
            continue
        lm = _IND_RE.match(line)
        if lm:
            name = lm.group(1).upper()
            key = ("I", name)
            ic_map.setdefault(key, 0.0)

    return [StateVar(k, n, v) for (k, n), v in ic_map.items()]


# ---------------------------------------------------------------------------
# IC injection
# ---------------------------------------------------------------------------

def inject_ics(src: Path, dst: Path, x: np.ndarray,
               state_vars: list[StateVar]) -> None:
    """
    Write a copy of *src* to *dst* with .IC values replaced by *x*.

    Existing .IC lines are removed; the new block is appended inside
    clearly marked sentinels so future calls can strip it cleanly.

    *x* must align with *state_vars* (same order as parse_state_vars).
    """
    if len(x) != len(state_vars):
        raise ValueError(
            f"State vector length {len(x)} != {len(state_vars)} state vars"
        )

    lines = src.read_text().splitlines()
    lines = _strip_ic_block(lines)

    # Remove any existing bare .IC lines (user-written ones)
    lines = [l for l in lines if not _IC_RE.match(l)]

    # Build new IC block
    ic_lines = [_IC_BLOCK_START]
    for sv, val in zip(state_vars, x):
        ic_lines.append(f".IC {sv.kind}({sv.name})={val:.10g}")
    ic_lines.append(_IC_BLOCK_END)

    # Insert before .END (or append if no .END)
    end_idx = _find_end(lines)
    if end_idx is not None:
        lines = lines[:end_idx] + ic_lines + lines[end_idx:]
    else:
        lines = lines + ic_lines

    dst.write_text("\n".join(lines) + "\n")


# ---------------------------------------------------------------------------
# .TRAN directive writer
# ---------------------------------------------------------------------------

def write_tran_directive(netlist: Path, tstop: float,
                         tstep: float | None = None) -> None:
    """
    Replace (or insert) the .TRAN directive in *netlist* (in-place).

    tstep defaults to tstop/500 if not supplied.
    """
    if tstep is None:
        tstep = tstop / 500.0

    tran_line = f".TRAN {_fmt(tstep)} {_fmt(tstop)}"

    text  = netlist.read_text()
    lines = text.splitlines()

    replaced = False
    new_lines = []
    for line in lines:
        if _TRAN_RE.match(line):
            if not replaced:
                new_lines.append(tran_line)
                replaced = True
            # drop duplicate .TRAN lines
        else:
            new_lines.append(line)

    if not replaced:
        end_idx = _find_end(new_lines)
        if end_idx is not None:
            new_lines.insert(end_idx, tran_line)
        else:
            new_lines.append(tran_line)

    netlist.write_text("\n".join(new_lines) + "\n")


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _strip_ic_block(lines: list[str]) -> list[str]:
    """Remove a previously written ltz IC block from a list of lines."""
    out   = []
    inside = False
    for line in lines:
        if line.strip() == _IC_BLOCK_START:
            inside = True
            continue
        if line.strip() == _IC_BLOCK_END:
            inside = False
            continue
        if not inside:
            out.append(line)
    return out


def _find_end(lines: list[str]) -> int | None:
    """Return index of the .END line, or None."""
    for i, line in enumerate(lines):
        if re.match(r"^\s*\.END\b", line, re.IGNORECASE):
            return i
    return None


def _fmt(t: float) -> str:
    """Format a time value compactly for a Xyce netlist."""
    for unit, scale in [("s", 1.0), ("ms", 1e-3), ("us", 1e-6),
                        ("ns", 1e-9), ("ps", 1e-12)]:
        if t >= scale * 0.999:
            val = t / scale
            # use integer if clean, otherwise up to 6 sig figs
            if abs(val - round(val)) < 1e-9 * val:
                return f"{int(round(val))}{unit}"
            return f"{val:.6g}{unit}"
    return f"{t:.6e}"
