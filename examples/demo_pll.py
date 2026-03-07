#!/usr/bin/env python3
"""
demo_pll.py — PLL lock acquisition demo via ltz ngspice shim + Xyce.

Runs an analog PLL (100kHz ref, 80kHz free-run VCO) through the
ngspice shared API backed by Xyce, then plots the waveforms.

Usage:
    python3 examples/demo_pll.py
"""

import os, sys, time
from ctypes import *

# --- Locate the shim ---
script_dir = os.path.dirname(os.path.abspath(__file__))
ltz_root = os.path.dirname(script_dir)
shim_path = os.path.join(ltz_root, 'lib', 'ngspice_shim', 'libngspice.so')

if not os.path.exists(shim_path):
    print(f"Error: shim not found at {shim_path}")
    print("Run: make -C lib/ngspice_shim")
    sys.exit(1)

# Re-exec with LD_LIBRARY_PATH if needed
xyce_build = os.environ.get('XYCE_BUILD', '/usr/local/src/xyce-build')
needed_dirs = [
    os.path.join(ltz_root, 'lib', 'ngspice_shim'),
    f'{xyce_build}/src',
    f'{xyce_build}/utils/XyceCInterface',
]
ld_path = os.environ.get('LD_LIBRARY_PATH', '')
missing = [d for d in needed_dirs if d not in ld_path]

if missing:
    os.environ['LD_LIBRARY_PATH'] = ':'.join(needed_dirs) + (':' + ld_path if ld_path else '')
    os.execv(sys.executable, [sys.executable] + sys.argv)

lib = CDLL(shim_path, RTLD_GLOBAL)

# --- Callbacks ---
SENDCHAR = CFUNCTYPE(c_int, c_char_p, c_int, c_void_p)
BGTRUN   = CFUNCTYPE(c_int, c_bool, c_int, c_void_p)

finished = [False]

def on_char(s, i, u):
    return 0

def on_bgt(done, i, u):
    finished[0] = bool(done)
    return 0

_cb_char = SENDCHAR(on_char)
_cb_bgt  = BGTRUN(on_bgt)

# --- Init ---
lib.ngSpice_Init(_cb_char, None, None, None, None, _cb_bgt, None)

# --- Load circuit ---
circuit_file = os.path.join(script_dir, 'pll.cir')
with open(circuit_file) as f:
    lines = [l.rstrip('\n') for l in f]

circ = (c_char_p * (len(lines) + 1))(*[l.encode() for l in lines], None)
rc = lib.ngSpice_Circ(circ)
if rc != 0:
    print("Failed to load circuit")
    sys.exit(1)

# --- Run ---
print("Running PLL simulation...")
t0 = time.time()
finished[0] = False
lib.ngSpice_Command(b"bg_run")

lib.ngSpice_running.restype = c_bool
while not finished[0] and lib.ngSpice_running():
    time.sleep(0.01)

elapsed = time.time() - t0
print(f"Done in {elapsed:.2f}s")

# --- Read vectors ---
class VectorInfo(Structure):
    _fields_ = [
        ("v_name", c_char_p), ("v_type", c_int), ("v_flags", c_short),
        ("v_realdata", POINTER(c_double)), ("v_compdata", c_void_p),
        ("v_length", c_int),
    ]

lib.ngGet_Vec_Info.restype = POINTER(VectorInfo)
lib.ngGet_Vec_Info.argtypes = [c_char_p]
lib.ngSpice_CurPlot.restype = c_char_p
lib.ngSpice_AllVecs.restype = POINTER(c_char_p)

plot = lib.ngSpice_CurPlot()
all_vecs = lib.ngSpice_AllVecs(plot)

vec_names = []
i = 0
while all_vecs[i]:
    vec_names.append(all_vecs[i].decode())
    i += 1

data = {}
for name in vec_names:
    vi = lib.ngGet_Vec_Info(name.encode())
    if vi:
        v = vi.contents
        data[name] = [v.v_realdata[j] for j in range(v.v_length)]

npts = len(data.get('time', []))
print(f"{npts} points, vectors: {', '.join(vec_names)}")

# --- Plot ---
try:
    import numpy as np
    import pyqtgraph as pg
    from PyQt5 import QtWidgets
except ImportError:
    print("PyQtGraph/PyQt5 not available — printing summary instead")
    for name in vec_names:
        d = data[name]
        peak = max(abs(x) for x in d) if d else 0
        print(f"  {name:12s}: {len(d)} pts, peak={peak:.4g}")
    sys.exit(0)

app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
pg.setConfigOptions(antialias=True, background='w', foreground='k')

win = pg.GraphicsLayoutWidget(title='ltz — PLL Lock Acquisition (Xyce)')
win.resize(1100, 700)

t_np = np.array(data['time'])
colors = [(31, 119, 180), (255, 127, 14), (44, 160, 44), (214, 39, 40)]

# Top plot: reference and VCO
p1 = win.addPlot(title='PLL Waveforms (100kHz ref, 80kHz VCO free-run)')
p1.showGrid(x=True, y=True, alpha=0.3)
p1.addLegend()
p1.setLabel('bottom', 'Time', units='s')
p1.setLabel('left', 'Voltage', units='V')

signal_plots = {'v(in)': 'Reference', 'v(vco)': 'VCO'}
ci = 0
for name in vec_names:
    if name in signal_plots:
        y = np.array(data[name])
        pen = pg.mkPen(color=colors[ci % len(colors)], width=2)
        p1.plot(t_np, y, pen=pen, name=signal_plots[name])
        ci += 1

# Bottom plot: control voltage
win.nextRow()
p2 = win.addPlot(title='Loop Filter Output (VCO Control Voltage)')
p2.showGrid(x=True, y=True, alpha=0.3)
p2.setLabel('bottom', 'Time', units='s')
p2.setLabel('left', 'Control Voltage', units='V')

if 'v(lpf)' in data:
    y = np.array(data['v(lpf)'])
    pen = pg.mkPen(color=colors[2], width=2)
    p2.plot(t_np, y, pen=pen)

p1.setXLink(p2)

win.show()
app.processEvents()

# Save screenshot
win.grab().save('/tmp/ltz_demo_pll.png')
print(f"Screenshot: /tmp/ltz_demo_pll.png")

for _ in range(50):
    app.processEvents()
    time.sleep(0.02)
