#!/usr/bin/env python3
"""
test_templates.py — Verify that RC filter template generation works.

PLL template tests have moved to kestrel/tests/test_behavioral.py.

Usage:
    python3 examples/test_templates.py
"""

import os, re, sys, tempfile, shutil

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPT_DIR)

from generate_examples import fill_template, RC_DEFAULTS, generate_all

PASS = 0
FAIL = 0

def check(name, condition, detail=''):
    global PASS, FAIL
    if condition:
        PASS += 1
        print(f"  PASS  {name}")
    else:
        FAIL += 1
        print(f"  FAIL  {name}" + (f" — {detail}" if detail else ''))


def test_rc_defaults():
    text = fill_template(os.path.join(SCRIPT_DIR, 'rc_filter.cir-template'),
                         RC_DEFAULTS)
    ref = open(os.path.join(SCRIPT_DIR, 'rc_filter.cir')).read()
    check('rc_filter.cir default == reference', text == ref,
          'generated text differs from rc_filter.cir')


def test_custom_params():
    params = dict(RC_DEFAULTS, R1='1k', C1='22n')
    text = fill_template(os.path.join(SCRIPT_DIR, 'rc_filter.cir-template'),
                         params)
    check('custom R1=1k appears', '1k' in text and '22n' in text)
    check('default 470 replaced for R1', text.count('470') == 1,
          f'expected 1 occurrence of 470 (R2), got {text.count("470")}')


def test_no_stale_placeholders():
    for tpl_name in ['rc_filter.cir-template', 'rc_filter.kicad_sch-template']:
        text = fill_template(os.path.join(SCRIPT_DIR, tpl_name), RC_DEFAULTS)
        stale = re.findall(r'@\w+@', text)
        check(f'{tpl_name}: no stale placeholders', len(stale) == 0,
              f'found: {stale}')


def test_kicad_parens():
    text = fill_template(os.path.join(SCRIPT_DIR, 'rc_filter.kicad_sch-template'),
                         RC_DEFAULTS)
    depth = 0
    for ch in text:
        if ch == '(':
            depth += 1
        elif ch == ')':
            depth -= 1
        if depth < 0:
            break
    check('rc_filter.kicad_sch-template: balanced parens', depth == 0,
          f'final depth={depth}')


def test_spice_directives():
    text = fill_template(os.path.join(SCRIPT_DIR, 'rc_filter.cir-template'),
                         RC_DEFAULTS)
    check('has .TRAN', '.TRAN' in text)
    check('has .PRINT', '.PRINT' in text)
    check('has .END', '.END' in text)


def test_generate_all():
    tmpdir = tempfile.mkdtemp(prefix='ltz_test_')
    try:
        for name in os.listdir(SCRIPT_DIR):
            src = os.path.join(SCRIPT_DIR, name)
            dst = os.path.join(tmpdir, name)
            if os.path.isdir(src):
                shutil.copytree(src, dst)
            else:
                shutil.copy2(src, dst)

        import generate_examples as ge
        orig_dir = ge.SCRIPT_DIR
        ge.SCRIPT_DIR = tmpdir
        try:
            ge.generate_all({'R1': '1k'})
        finally:
            ge.SCRIPT_DIR = orig_dir

        rc_cir = open(os.path.join(tmpdir, 'rc_filter.cir')).read()
        check('generate_all: R1=1k in rc_filter.cir', 'R1 in mid 1k' in rc_cir)
    finally:
        shutil.rmtree(tmpdir)


def test_missing_param():
    try:
        fill_template(os.path.join(SCRIPT_DIR, 'rc_filter.cir-template'),
                      {'V_AMPLITUDE': '5'})
        check('missing params raises ValueError', False, 'no exception raised')
    except ValueError:
        check('missing params raises ValueError', True)


if __name__ == '__main__':
    print("=== ltz RC filter template tests ===\n")
    test_rc_defaults()
    test_custom_params()
    test_no_stale_placeholders()
    test_kicad_parens()
    test_spice_directives()
    test_generate_all()
    test_missing_param()
    print(f"\n{PASS} passed, {FAIL} failed")
    sys.exit(1 if FAIL else 0)
