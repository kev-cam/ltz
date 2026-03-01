#!/usr/bin/env python3
"""
ltz_convert.py — LTspice to Xyce netlist converter

Translates LTspice SPICE netlists (.cir/.net) into Xyce-compatible format.
Part of the ltz project: https://github.com/kev-cam/ltz

Usage:
    python ltz_convert.py input.cir                    # convert single file
    python ltz_convert.py input.cir -o output.cir      # convert to specific output
    python ltz_convert.py --scan dir/                   # scan directory, report compatibility
    python ltz_convert.py --batch dir/ -o outdir/       # batch convert all .cir files
"""

import argparse
import os
import re
import sys
from pathlib import Path
from dataclasses import dataclass, field
from typing import List, Tuple


@dataclass
class ConversionReport:
    """Track what was changed and what might be problematic."""
    source: str = ""
    changes: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    errors: List[str] = field(default_factory=list)
    self_contained: bool = True

    @property
    def status(self) -> str:
        if self.errors:
            return "ERROR"
        if self.warnings:
            return "WARN"
        return "OK"


def convert_ltspice_to_xyce(lines: List[str], report: ConversionReport) -> List[str]:
    """
    Convert LTspice SPICE netlist lines to Xyce-compatible format.

    Key translations:
    - .PROBE → removed (Xyce uses .PRINT)
    - .PLOT with no args → removed
    - .lib with Windows paths → commented out with warning
    - .END → .END (kept, but Xyce is less strict about it)
    - .BACKANNO → removed (LTspice-specific)
    - .model D D → needs proper model params for Xyce
    - Bare model references (1N4148 etc) → need .model or .lib
    - VALUE = {} behavioral sources → Xyce B-source syntax
    - .func → Xyce .FUNC (compatible but case-sensitive)
    - .meas → .MEASURE (Xyce syntax)
    - ** → ; for exponent (context-dependent)
    - .PRINT TRAN V(1) → compatible as-is
    """
    output = []
    has_end = False
    has_print = False
    analysis_type = None

    # First pass: detect analysis type
    for line in lines:
        stripped = line.strip()
        upper = stripped.upper()
        if upper.startswith('.TRAN'):
            analysis_type = 'TRAN'
        elif upper.startswith('.AC'):
            analysis_type = 'AC'
        elif upper.startswith('.DC'):
            analysis_type = 'DC'
        elif upper.startswith('.OP'):
            analysis_type = 'DC'
        elif upper.startswith('.TF'):
            analysis_type = 'DC'

    # Second pass: convert
    for i, line in enumerate(lines, 1):
        stripped = line.strip()
        upper = stripped.upper()

        # Skip empty lines (preserve them)
        if not stripped:
            output.append(line)
            continue

        # --- Remove LTspice-specific directives ---

        # .PROBE — LTspice-only, Xyce uses .PRINT
        if upper == '.PROBE' or upper.startswith('.PROBE '):
            report.changes.append(f"L{i}: Removed .PROBE (LTspice-specific)")
            output.append(f"* [ltz] removed: {stripped}")
            continue

        # .PLOT with no arguments — LTspice-only
        if upper == '.PLOT':
            report.changes.append(f"L{i}: Removed bare .PLOT")
            output.append(f"* [ltz] removed: {stripped}")
            continue

        # .BACKANNO — LTspice-specific
        if upper.startswith('.BACKANNO'):
            report.changes.append(f"L{i}: Removed .BACKANNO")
            output.append(f"* [ltz] removed: {stripped}")
            continue

        # --- Handle .lib with Windows paths ---
        if upper.startswith('.LIB') and '\\' in stripped:
            report.warnings.append(
                f"L{i}: Windows .lib path removed — model may be missing: {stripped}"
            )
            report.self_contained = False
            output.append(f"* [ltz] removed Windows path: {stripped}")
            continue

        # --- Handle .include with local files ---
        if upper.startswith('.INCLUDE') or upper.startswith('.INC'):
            inc_file = stripped.split(None, 1)[1] if len(stripped.split()) > 1 else ""
            if '\\' in inc_file:
                report.warnings.append(f"L{i}: Windows .include path: {stripped}")
                output.append(f"* [ltz] removed Windows path: {stripped}")
                report.self_contained = False
            else:
                # Local include — keep but warn if file doesn't exist
                report.warnings.append(f"L{i}: .include dependency: {inc_file}")
                output.append(line)
            continue

        # --- Handle empty .model statements ---
        # LTspice: .model D D  (uses built-in defaults)
        # Xyce needs actual parameters
        match = re.match(r'^\.model\s+(\S+)\s+D\s*$', stripped, re.IGNORECASE)
        if match:
            model_name = match.group(1)
            report.warnings.append(
                f"L{i}: Empty diode model '{model_name}' — Xyce needs parameters"
            )
            output.append(f".model {model_name} D(IS=2.52e-9 RS=0.568 N=1.752 BV=100 IBV=100u)")
            report.changes.append(f"L{i}: Added default diode params to .model {model_name}")
            continue

        # --- Handle .PLOT with arguments (convert to .PRINT) ---
        plot_match = re.match(r'^\.PLOT\s+(.+)', stripped, re.IGNORECASE)
        if plot_match:
            args = plot_match.group(1)
            output.append(f".PRINT {args}")
            report.changes.append(f"L{i}: .PLOT → .PRINT")
            has_print = True
            continue

        # --- Handle .meas → .MEASURE ---
        if upper.startswith('.MEAS ') or upper.startswith('.MEAS\t'):
            converted = re.sub(r'^\.meas', '.MEASURE', stripped, flags=re.IGNORECASE)
            # Xyce uses FROM/TO instead of "From ... to ..."
            converted = re.sub(r'\bFrom\b', 'FROM', converted)
            converted = re.sub(r'\bto\b', 'TO', converted)
            output.append(converted)
            report.changes.append(f"L{i}: .meas → .MEASURE")
            continue

        # --- Handle .func (mostly compatible, just note it) ---
        if upper.startswith('.FUNC'):
            # LTspice uses ** for power, Xyce uses **
            # Actually both support **, so this is compatible
            # But LTspice also supports ^ which Xyce doesn't
            if '^' in stripped:
                converted = stripped.replace('^', '**')
                output.append(converted)
                report.changes.append(f"L{i}: Replaced ^ with ** in .func")
            else:
                output.append(line)
            continue

        # --- Handle behavioral sources ---
        # LTspice: E/Gname n+ n- VALUE = { expr }
        # Xyce: B source syntax: Bname n+ n- V={expr} or I={expr}
        value_match = re.match(
            r'^([EG])(\S+)\s+(\S+)\s+(\S+)\s+VALUE\s*=\s*\{(.+)\}',
            stripped, re.IGNORECASE
        )
        if value_match:
            etype = value_match.group(1).upper()
            name = value_match.group(2)
            nplus = value_match.group(3)
            nminus = value_match.group(4)
            expr = value_match.group(5).strip()
            # Xyce supports E/G with VALUE syntax, but B-source is more robust
            # For now, keep the E/G VALUE syntax — Xyce supports it
            output.append(stripped)
            report.changes.append(f"L{i}: Behavioral source {etype}{name} — kept E/G VALUE syntax")
            continue

        # --- Handle .PRINT with broken syntax ---
        print_match = re.match(r'^\.PRINT', stripped, re.IGNORECASE)
        if print_match:
            has_print = True
            output.append(line)
            continue

        # --- Track .END ---
        if upper == '.END':
            has_end = True
            output.append(line)
            continue

        # --- Handle LTspice semicolon comments ---
        # LTspice uses ; for inline comments, Xyce uses $ or ;
        # Actually Xyce supports ; too, so this is compatible
        # But LTspice also uses ; at start of line as a comment
        if stripped.startswith(';'):
            output.append('* ' + stripped[1:])
            report.changes.append(f"L{i}: ; comment → * comment")
            continue

        # --- Pass through everything else ---
        output.append(line)

    # --- Post-processing ---

    # Add .PRINT if none exists and we know the analysis type
    if not has_print and analysis_type:
        report.warnings.append("No .PRINT statement — Xyce needs explicit output specification")

    # Ensure .END exists
    if not has_end:
        output.append('.END')
        report.changes.append("Added missing .END")

    return output


def scan_file(filepath: str) -> ConversionReport:
    """Scan a file and report compatibility without converting."""
    report = ConversionReport(source=filepath)

    try:
        with open(filepath, 'r', errors='replace') as f:
            lines = f.readlines()
    except Exception as e:
        report.errors.append(f"Cannot read file: {e}")
        return report

    # Check for binary content
    try:
        with open(filepath, 'rb') as f:
            raw = f.read(512)
            if b'\x00' in raw:
                report.errors.append("Binary file — not a text netlist")
                return report
    except:
        pass

    for i, line in enumerate(lines, 1):
        stripped = line.strip()
        upper = stripped.upper()

        if upper.startswith('.LIB') and '\\' in stripped:
            report.warnings.append(f"L{i}: Windows .lib path")
            report.self_contained = False
        if upper.startswith('.INCLUDE') and '\\' in stripped:
            report.warnings.append(f"L{i}: Windows .include path")
            report.self_contained = False
        if upper.startswith('.INCLUDE') and '\\' not in stripped:
            inc_file = stripped.split(None, 1)[1] if len(stripped.split()) > 1 else ""
            report.warnings.append(f"L{i}: Local .include: {inc_file}")
            report.self_contained = False
        if upper == '.PROBE' or upper.startswith('.PROBE '):
            report.changes.append(f"L{i}: .PROBE needs removal")
        if re.match(r'^\.model\s+\S+\s+D\s*$', stripped, re.IGNORECASE):
            report.warnings.append(f"L{i}: Empty model — needs parameters")

    return report


def convert_file(inpath: str, outpath: str = None) -> ConversionReport:
    """Convert a single file."""
    report = ConversionReport(source=inpath)

    try:
        with open(inpath, 'r', errors='replace') as f:
            lines = f.readlines()
    except Exception as e:
        report.errors.append(f"Cannot read: {e}")
        return report

    # Check for binary
    try:
        with open(inpath, 'rb') as f:
            if b'\x00' in f.read(512):
                report.errors.append("Binary file — skipping")
                return report
    except:
        pass

    converted = convert_ltspice_to_xyce(lines, report)

    if outpath is None:
        outpath = str(Path(inpath).with_suffix('.xyce.cir'))

    os.makedirs(os.path.dirname(outpath) or '.', exist_ok=True)
    with open(outpath, 'w') as f:
        f.writelines(l if l.endswith('\n') else l + '\n' for l in converted)

    return report


def main():
    parser = argparse.ArgumentParser(
        description='ltz — LTspice to Xyce netlist converter'
    )
    parser.add_argument('input', nargs='?', help='Input .cir file or directory')
    parser.add_argument('-o', '--output', help='Output file or directory')
    parser.add_argument('--scan', action='store_true',
                        help='Scan and report compatibility without converting')
    parser.add_argument('--batch', action='store_true',
                        help='Batch convert all .cir files in directory')
    parser.add_argument('--self-contained', action='store_true',
                        help='Only process self-contained files (no external deps)')
    parser.add_argument('-v', '--verbose', action='store_true',
                        help='Verbose output')
    args = parser.parse_args()

    if not args.input:
        parser.print_help()
        sys.exit(1)

    input_path = Path(args.input)

    # Collect files
    if input_path.is_dir():
        files = sorted(input_path.rglob('*.cir'))
    elif input_path.is_file():
        files = [input_path]
    else:
        print(f"Error: {input_path} not found", file=sys.stderr)
        sys.exit(1)

    # Process
    stats = {'ok': 0, 'warn': 0, 'error': 0, 'total': 0}

    for filepath in files:
        stats['total'] += 1

        if args.scan:
            report = scan_file(str(filepath))
        else:
            if args.output and input_path.is_dir():
                rel = filepath.relative_to(input_path)
                outpath = str(Path(args.output) / rel)
            elif args.output:
                outpath = args.output
            else:
                outpath = None

            report = convert_file(str(filepath), outpath)

        if args.self_contained and not report.self_contained:
            stats['total'] -= 1
            continue

        # Tally
        if report.errors:
            stats['error'] += 1
        elif report.warnings:
            stats['warn'] += 1
        else:
            stats['ok'] += 1

        # Output
        status_icon = {'OK': '✓', 'WARN': '⚠', 'ERROR': '✗'}[report.status]
        print(f"  {status_icon} [{report.status:5}] {filepath}")

        if args.verbose:
            for c in report.changes:
                print(f"           change: {c}")
            for w in report.warnings:
                print(f"           warn:   {w}")
            for e in report.errors:
                print(f"           error:  {e}")

    # Summary
    print(f"\n{'─' * 60}")
    print(f"  Total: {stats['total']}  "
          f"OK: {stats['ok']}  "
          f"Warnings: {stats['warn']}  "
          f"Errors: {stats['error']}")
    if stats['total'] > 0:
        pct = (stats['ok'] + stats['warn']) / stats['total'] * 100
        print(f"  Convertible: {pct:.0f}%")
    print(f"{'─' * 60}")


if __name__ == '__main__':
    main()
