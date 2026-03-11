#!/usr/bin/env python3
"""
generate_examples.py — Fill in parameterized RC filter templates.

Reads *-template files, substitutes @PARAM@ placeholders, writes the
concrete .cir and .kicad_sch files.

PLL generation has moved to the kestrel project (kestrel.models.behavioral).

Usage:
    python3 examples/generate_examples.py                  # defaults
    python3 examples/generate_examples.py --r1 1k          # override one param
"""

import argparse, os, re, sys

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))

# ── Parameter definitions (name → default) ──────────────────────────

RC_DEFAULTS = {
    'V_AMPLITUDE': '3.3',
    'V_FREQUENCY': '10k',
    'R1':          '470',
    'C1':          '10n',
    'R2':          '470',
    'C2':          '10n',
    'TRAN_STEP':   '0.5u',
    'TRAN_STOP':   '500u',
}

# ── Template engine ──────────────────────────────────────────────────

def fill_template(template_path, params):
    """Read a template, replace @PARAM@ placeholders, return the result."""
    with open(template_path) as f:
        text = f.read()

    found = set(re.findall(r'@(\w+)@', text))
    missing = found - set(params)
    if missing:
        raise ValueError(f"{template_path}: undefined parameters: {missing}")

    for name, value in params.items():
        text = text.replace(f'@{name}@', value)

    return text


def generate(template_name, output_name, params):
    """Fill a template and write the output file."""
    tpl = os.path.join(SCRIPT_DIR, template_name)
    out = os.path.join(SCRIPT_DIR, output_name)
    text = fill_template(tpl, params)
    with open(out, 'w') as f:
        f.write(text)
    print(f"  {output_name}")


def generate_all(rc_overrides=None):
    """Generate all concrete circuit files from templates."""
    rc = dict(RC_DEFAULTS, **(rc_overrides or {}))

    print("Generating:")
    generate('rc_filter.cir-template',       'rc_filter.cir',              rc)
    generate('rc_filter.kicad_sch-template',  'rc_filter/rc_filter.kicad_sch', rc)
    print("Done.")


# ── CLI ──────────────────────────────────────────────────────────────

def main():
    p = argparse.ArgumentParser(description='Generate ltz RC filter example from templates')

    for name, default in RC_DEFAULTS.items():
        p.add_argument(f'--{name.lower().replace("_", "-")}',
                       default=default, metavar='VAL',
                       help=f'{name} (default: {default})')

    args = p.parse_args()

    rc_overrides = {}
    for name in RC_DEFAULTS:
        val = getattr(args, name.lower())
        if val != RC_DEFAULTS[name]:
            rc_overrides[name] = val

    generate_all(rc_overrides)


if __name__ == '__main__':
    main()
