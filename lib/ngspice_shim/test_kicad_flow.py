#!/usr/bin/env python3
"""
Test the ngspice shim using the exact same call sequence KiCad uses.

This mimics KiCad's NGSPICE::init_dll() → LoadNetlist() → Run() →
IsRunning() → AllVectors() → GetRealVector() flow.
"""

import os, sys, time, ctypes
from ctypes import *

SHIM = os.path.join(os.path.dirname(__file__), 'libngspice.so')

# KiCad uses wxDynamicLibrary which is dlopen(). Replicate that.
lib = ctypes.cdll.LoadLibrary(SHIM)

# --- Callback types (matching sharedspice.h) ---
SENDCHAR   = CFUNCTYPE(c_int, c_char_p, c_int, c_void_p)
SENDSTAT   = CFUNCTYPE(c_int, c_char_p, c_int, c_void_p)
CTRL_EXIT  = CFUNCTYPE(c_int, c_int, c_bool, c_bool, c_int, c_void_p)
SENDDATA   = CFUNCTYPE(c_int, c_void_p, c_int, c_int, c_void_p)
SENDINIT   = CFUNCTYPE(c_int, c_void_p, c_int, c_void_p)
BGTRUN     = CFUNCTYPE(c_int, c_bool, c_int, c_void_p)

sim_finished = [False]
output_lines = []

def cb_sendchar(s, ident, user):
    msg = s.decode() if s else ''
    # KiCad strips "stdout " / "stderr " prefix
    if msg.startswith('stdout '):
        msg = msg[7:]
    elif msg.startswith('stderr '):
        msg = msg[7:]
    output_lines.append(msg)
    return 0

def cb_sendstat(s, ident, user):
    return 0

def cb_ctrl_exit(status, immediate, quit_flag, ident, user):
    print(f"  [ControlledExit: status={status}]")
    return 0

def cb_bgtrun(finished, ident, user):
    sim_finished[0] = bool(finished)
    return 0

# Keep references to prevent GC
_cb_char = SENDCHAR(cb_sendchar)
_cb_stat = SENDSTAT(cb_sendstat)
_cb_exit = CTRL_EXIT(cb_ctrl_exit)
_cb_bgt  = BGTRUN(cb_bgtrun)

# --- Step 1: ngSpice_Init (KiCad's init_dll) ---
# KiCad passes NULL for SendData and SendInitData
print("Step 1: ngSpice_Init")
lib.ngSpice_Init(_cb_char, _cb_stat, _cb_exit,
                 None, None,  # SendData=NULL, SendInitData=NULL (KiCad does this)
                 _cb_bgt, None)

# --- Step 2: Commands (KiCad's init_dll post-init) ---
print("Step 2: Post-init commands")
lib.ngSpice_Command(b"set noaskquit")
lib.ngSpice_Command(b"set nomoremode")

# Load empty circuit first (KiCad does this)
empty = (c_char_p * 3)(b"*", b".end", None)
lib.ngSpice_Circ(empty)

# --- Step 3: ngSpice_Command("reset") (KiCad's Init()) ---
print("Step 3: Reset")
lib.ngSpice_Command(b"reset")

# --- Step 4: LoadNetlist (KiCad's LoadNetlist) ---
print("Step 4: LoadNetlist")
lib.ngSpice_Command(b"remcirc")

# KiCad sends netlist as array of char* lines
netlist_lines = [
    b"* KiCad simulation test",
    b"V1 in 0 SIN(0 3.3 10k)",
    b"R1 in mid 470",
    b"C1 mid 0 10n",
    b"R2 mid out 470",
    b"C2 out 0 10n",
    b".tran 1u 500u",
    b".print tran V(in) V(mid) V(out) I(R1)",
    b".end",
]
circ = (c_char_p * (len(netlist_lines) + 1))(*netlist_lines, None)
rc = lib.ngSpice_Circ(circ)
assert rc == 0, f"ngSpice_Circ failed: {rc}"

# --- Step 5: Run (KiCad's Run()) ---
print("Step 5: bg_run")
sim_finished[0] = False
lib.ngSpice_Command(b"bg_run")

# --- Step 6: Poll IsRunning (KiCad's IsRunning()) ---
lib.ngSpice_running.restype = c_bool
t0 = time.time()
while not sim_finished[0]:
    running = lib.ngSpice_running()
    if not running:
        break
    time.sleep(0.05)
    if time.time() - t0 > 30:
        print("ERROR: timeout")
        sys.exit(1)

elapsed = time.time() - t0
print(f"  Simulation completed in {elapsed:.2f}s")

# --- Step 7: Get results (KiCad's AllVectors + GetRealVector) ---
print("Step 7: Read results")

# CurPlot
lib.ngSpice_CurPlot.restype = c_char_p
plot_name = lib.ngSpice_CurPlot()
print(f"  CurPlot: {plot_name.decode()}")

# AllVecs
lib.ngSpice_AllVecs.restype = POINTER(c_char_p)
all_vecs = lib.ngSpice_AllVecs(plot_name)
vec_names = []
i = 0
while all_vecs[i]:
    vec_names.append(all_vecs[i].decode())
    i += 1
print(f"  Vectors: {', '.join(vec_names)}")

# GetRealVector via ngGet_Vec_Info (KiCad's GetRealVector pattern)
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

# KiCad's NGSPICE_LOCK_REALLOC pattern
lib.ngSpice_LockRealloc()

results = {}
for name in vec_names:
    vi = lib.ngGet_Vec_Info(name.encode())
    if vi:
        v = vi.contents
        n = v.v_length
        # KiCad copies data into std::vector immediately
        data = [v.v_realdata[j] for j in range(n)]
        results[name] = data
        peak = max(abs(x) for x in data) if data else 0
        print(f"  {name:12s}: {n} pts, peak={peak:.4g}")

lib.ngSpice_UnlockRealloc()

# --- Verify ---
print("\nVerification:")
assert 'time' in results, "Missing 'time' vector"
assert len(results['time']) > 50, f"Too few points: {len(results['time'])}"

t = results['time']
for i in range(1, len(t)):
    assert t[i] > t[i-1], f"Time not monotonic at {i}"

vin = results.get('v(in)', [])
assert len(vin) > 0, "Missing v(in)"
vin_peak = max(abs(x) for x in vin)
assert 2.5 < vin_peak < 3.5, f"v(in) peak {vin_peak} unexpected (expect ~3.3)"

vmid = results.get('v(mid)', [])
assert len(vmid) > 0, "Missing v(mid)"

vout = results.get('v(out)', [])
assert len(vout) > 0, "Missing v(out)"
vout_peak = max(abs(x) for x in vout)
assert vout_peak < vin_peak, "v(out) should be attenuated vs v(in)"

print(f"  {len(t)} points, time [0 .. {t[-1]:.6g}]")
print(f"  v(in) peak = {vin_peak:.4f}V")
print(f"  v(out) peak = {vout_peak:.4f}V (attenuated by two-stage RC)")

# Check we got current too
ir1 = results.get('i(r1)', results.get('I(R1)', []))
if ir1:
    ir1_peak = max(abs(x) for x in ir1)
    print(f"  i(r1) peak = {ir1_peak:.6g}A")

print("\n=== KiCad flow test PASSED ===")
