#!/usr/bin/env python3
"""Smoke test for the ngspice shim (Xyce backend)."""

import os, sys, time
from ctypes import *

# Load the shim
lib = CDLL('./libngspice.so', RTLD_GLOBAL)

# Callback types matching sharedspice.h
SENDCHAR  = CFUNCTYPE(c_int, c_char_p, c_int, c_void_p)
SENDSTAT  = CFUNCTYPE(c_int, c_char_p, c_int, c_void_p)
CTRL_EXIT = CFUNCTYPE(c_int, c_int, c_bool, c_bool, c_int, c_void_p)
BGTRUN    = CFUNCTYPE(c_int, c_bool, c_int, c_void_p)

messages = []

def on_char(s, i, u):
    msg = s.decode() if s else ''
    messages.append(msg)
    print(f'  [{msg.strip()}]')
    return 0

def on_bgtrun(finished, i, u):
    print(f'  [BGThread: {"finished" if finished else "started"}]')
    return 0

cb_char  = SENDCHAR(on_char)
cb_bgt   = BGTRUN(on_bgtrun)

# Init
print("=== ngSpice_Init ===")
lib.ngSpice_Init(cb_char, None, None, None, None, cb_bgt, None)

# Load circuit
print("\n=== ngSpice_Circ ===")
lines = [
    b'* RC test circuit',
    b'V1 in 0 SIN(0 5 1k)',
    b'R1 in out 1k',
    b'C1 out 0 0.1u',
    b'.TRAN 1u 2m',
    b'.PRINT TRAN V(in) V(out)',
    b'.END',
]
circ = (c_char_p * (len(lines) + 1))(*lines, None)
rc = lib.ngSpice_Circ(circ)
print(f"  Circ returned: {rc}")
assert rc == 0, f"ngSpice_Circ failed with {rc}"

# Check CurPlot before run
lib.ngSpice_CurPlot.restype = c_char_p
plot = lib.ngSpice_CurPlot()
print(f"  CurPlot (before run): {plot}")

# Run simulation
print("\n=== ngSpice_Command('bg_run') ===")
lib.ngSpice_Command(b"bg_run")

# Wait for completion
lib.ngSpice_running.restype = c_bool
timeout = 30
t0 = time.time()
while lib.ngSpice_running():
    time.sleep(0.1)
    if time.time() - t0 > timeout:
        print("ERROR: Simulation timed out!")
        sys.exit(1)

elapsed = time.time() - t0
print(f"  Simulation completed in {elapsed:.2f}s")

# Check plot and vectors
print("\n=== Results ===")
plot = lib.ngSpice_CurPlot()
print(f"  CurPlot: {plot}")

lib.ngSpice_AllPlots.restype = POINTER(c_char_p)
all_plots = lib.ngSpice_AllPlots()
print("  AllPlots:", end="")
i = 0
while all_plots[i]:
    print(f" {all_plots[i].decode()}", end="")
    i += 1
print()

lib.ngSpice_AllVecs.restype = POINTER(c_char_p)
all_vecs = lib.ngSpice_AllVecs(plot)
print("  AllVecs:", end="")
vec_names = []
i = 0
while all_vecs[i]:
    name = all_vecs[i].decode()
    vec_names.append(name)
    print(f" {name}", end="")
    i += 1
print()

# Get vector data
class VectorInfo(Structure):
    _fields_ = [
        ("v_name", c_char_p),
        ("v_type", c_int),
        ("v_flags", c_short),
        ("v_realdata", POINTER(c_double)),
        ("v_compdata", c_void_p),
        ("v_length", c_int),
    ]

lib.ngGet_Vec_Info.restype = POINTER(VectorInfo)
lib.ngGet_Vec_Info.argtypes = [c_char_p]

for name in vec_names:
    vi = lib.ngGet_Vec_Info(name.encode())
    if vi:
        v = vi.contents
        print(f"  {v.v_name.decode():12s}: {v.v_length} points", end="")
        if v.v_realdata and v.v_length > 0:
            first = v.v_realdata[0]
            last = v.v_realdata[v.v_length - 1]
            # Find peak
            peak = max(abs(v.v_realdata[i]) for i in range(v.v_length))
            print(f"  [{first:.6g} ... {last:.6g}]  peak={peak:.4g}", end="")
        print()
    else:
        print(f"  {name}: NOT FOUND")

# Verify data quality — must copy data before next ngGet_Vec_Info call
# (the returned pointer is to a static struct, same as real ngspice)
def get_real_data(name):
    vi = lib.ngGet_Vec_Info(name)
    if not vi:
        return None, 0
    v = vi.contents
    n = v.v_length
    data = [v.v_realdata[i] for i in range(n)]
    return data, n

t_data, t_len = get_real_data(b"time")
vin_data, vin_len = get_real_data(b"v(in)")
vout_data, vout_len = get_real_data(b"v(out)")

assert t_len > 100, f"Too few points: {t_len}"
assert t_len == vin_len == vout_len, "Length mismatch"

# V(in) should peak near 5V (SIN amplitude)
vin_peak = max(abs(v) for v in vin_data)
assert 4.5 < vin_peak < 5.1, f"V(in) peak {vin_peak} not near 5V"

# V(out) should be attenuated (RC filter)
vout_peak = max(abs(v) for v in vout_data)
assert 0.5 < vout_peak < 5.0, f"V(out) peak {vout_peak} not reasonable"
assert vout_peak < vin_peak, "V(out) should be attenuated"

# Time should be monotonically increasing
for i in range(1, t_len):
    assert t_data[i] > t_data[i-1], \
        f"Time not monotonic at {i}: {t_data[i-1]} >= {t_data[i]}"

print(f"\n  Verified: {t_len} points, V(in) peak={vin_peak:.3f}V, V(out) peak={vout_peak:.3f}V")
print("\n=== PASS ===")
