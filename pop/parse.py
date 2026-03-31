"""
ltz/pop/parse.py

Parse Xyce simulation output for POP analysis:
  - read_prn()           load a .prn file into numpy arrays
  - read_final_state()   extract final-timestep state vector
  - read_state_history() extract full time history (for waveform plotting)

Xyce .prn format
----------------
The default Xyce print format is whitespace-delimited with a header line:

    Index  TIME  V(VOUT)  V(SW)  I(L1)  ...
    0      0.0   0.0      0.0    0.0    ...
    1      2e-9  0.001    12.0   0.002  ...
    ...
    End of Xyce(TM) Simulation

Columns may be in any order.  The Index column is always present.
When .STEP or .MEASURE are active, multiple sweep blocks appear separated
by blank lines — read_prn() handles this by concatenating them so the
caller always sees a single time axis.

We also handle the Dakota/parallel variant where the header may be
repeated for each step block.
"""

from __future__ import annotations

import re
from pathlib import Path

import numpy as np


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def read_prn(path: Path) -> dict[str, np.ndarray]:
    """
    Parse a Xyce .prn file.

    Returns a dict mapping column name → 1-D numpy array.
    Column names are normalised: 'TIME', 'V(VOUT)', 'I(L1)', etc.
    The 'Index' column is included as 'INDEX'.
    """
    text = path.read_text(errors="replace")
    return _parse_prn_text(text)


def read_final_state(prn_path: Path,
                     state_vars: list) -> np.ndarray:
    """
    Extract the final-timestep values of *state_vars* from a .prn file.

    *state_vars* is a list of StateVar(kind, name, ic) as produced by
    netlist.parse_state_vars().

    Returns a numpy array aligned with state_vars.
    """
    data = read_prn(prn_path)
    result = np.zeros(len(state_vars))

    for i, sv in enumerate(state_vars):
        col = f"{sv.kind}({sv.name})"
        # Xyce may upper- or lower-case node names; try both
        val = _lookup_last(data, col)
        if val is None:
            val = _lookup_last(data, col.upper())
        if val is None:
            val = _lookup_last(data, col.lower())
        if val is None:
            raise KeyError(
                f"Column {col!r} not found in {prn_path}.\n"
                f"Available columns: {sorted(data.keys())}"
            )
        result[i] = val

    return result


def read_state_history(prn_path: Path,
                       state_vars: list) -> tuple[np.ndarray, np.ndarray]:
    """
    Return (time, matrix) for the full waveform of each state variable.

    *matrix* has shape (len(state_vars), len(time)).
    Useful for convergence plots and waveform display after POP.
    """
    data = read_prn(prn_path)

    time = data.get("TIME") if "TIME" in data else data.get("time")
    if time is None:
        raise KeyError(f"No TIME column in {prn_path}")

    rows = []
    for sv in state_vars:
        col = f"{sv.kind}({sv.name})"
        val = _lookup_col(data, col)
        if val is None:
            val = _lookup_col(data, col.upper())
        if val is None:
            val = _lookup_col(data, col.lower())
        if val is None:
            raise KeyError(f"Column {col!r} not found in {prn_path}")
        rows.append(val)

    return time, np.vstack(rows)


# ---------------------------------------------------------------------------
# Internal parsing
# ---------------------------------------------------------------------------

_END_RE   = re.compile(r"End of Xyce", re.IGNORECASE)
_BLANK_RE = re.compile(r"^\s*$")


def _parse_prn_text(text: str) -> dict[str, np.ndarray]:
    """
    Core parser.  Handles:
      - single simulation block
      - multi-block .STEP output (header repeated per block)
      - trailing 'End of Xyce(TM) Simulation' sentinel
    """
    lines = text.splitlines()

    header: list[str] | None = None
    blocks: list[list[list[float]]] = []   # list of blocks, each a list of rows
    current: list[list[float]] = []

    for line in lines:
        if _END_RE.search(line):
            break
        if _BLANK_RE.match(line):
            if current:
                blocks.append(current)
                current = []
            continue

        stripped = line.strip()
        if not stripped:
            continue

        # Check if this is a header line (first token is 'Index' or 'index')
        tokens = stripped.split()
        if tokens[0].lower() == "index":
            if current:                     # save block before new header
                blocks.append(current)
                current = []
            header = [_normalise_col(t) for t in tokens]
            continue

        # Data row
        if header is None:
            continue                        # haven't seen a header yet
        try:
            row = [float(t) for t in tokens]
        except ValueError:
            continue                        # skip malformed lines
        if len(row) == len(header):
            current.append(row)

    if current:
        blocks.append(current)

    if not blocks or header is None:
        raise ValueError("No data found in .prn file")

    # Concatenate all blocks (deduplicate repeated time=0 at block boundaries)
    all_rows: list[list[float]] = []
    time_col = header.index("TIME") if "TIME" in header else None

    for b_idx, block in enumerate(blocks):
        for r_idx, row in enumerate(block):
            if b_idx > 0 and r_idx == 0 and time_col is not None:
                # skip if t==0 restart row that duplicates end of previous block
                if all_rows and row[time_col] <= all_rows[-1][time_col]:
                    continue
            all_rows.append(row)

    arr = np.array(all_rows, dtype=float)   # shape: (nrows, ncols)

    return {col: arr[:, i] for i, col in enumerate(header)}


def _normalise_col(token: str) -> str:
    """
    Normalise a column header token to a canonical form.

    Xyce uses e.g. 'V(vout)' or 'I(L1)' or 'TIME'.
    We upper-case the outer part but preserve node/element name case
    as-is because netlists can be mixed-case.

    Special cases:
      Index → INDEX
      TIME  → TIME
      V(x)  → V(x)   (kind upper, content preserved)
      I(x)  → I(x)
    """
    if token.lower() == "index":
        return "INDEX"
    if token.upper() == "TIME":
        return "TIME"
    # V(...) / I(...) / P(...) etc.
    m = re.match(r"^([A-Za-z]+)\((.+)\)$", token)
    if m:
        return f"{m.group(1).upper()}({m.group(2)})"
    return token.upper()


def _lookup_last(data: dict[str, np.ndarray], col: str) -> float | None:
    arr = data.get(col)
    if arr is not None and len(arr):
        return float(arr[-1])
    return None


def _lookup_col(data: dict[str, np.ndarray], col: str) -> np.ndarray | None:
    return data.get(col)
