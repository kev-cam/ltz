#!/usr/bin/env python3
"""
xyce_cli - Interactive CLI for Xyce circuit simulation

An ngspice-like interactive REPL for the Xyce SPICE engine, built on
Xyce's C interface (libxycecinterface.so) via ctypes.

Usage:
    xyce_cli.py [netlist.cir]
    xyce_cli.py --lib /path/to/lib [netlist.cir]

Commands:
    source <file>       Load a netlist (or reload current)
    run                 Run simulation to completion
    step [time]         Advance simulation by time delta (default: 1/100 of tran stop)
    stop                Halt a running simulation (or Ctrl-C)
    print <expr>        Show current value of V(node) or I(device)
    show                List all node voltages at current time
    alter <dev> <val>   Change device parameter (e.g., alter R1 1k)
    status              Show simulation time, progress, state
    reset               Restart simulation from t=0
    devices [type]      List all devices (optionally filter by type)
    param <dev:param>   Query device parameter value
    write [file.raw]    Write accumulated waveform data to .raw file
    help                Show this help
    quit / exit         Exit
"""

import sys
import os
import re
import struct
import time
import tempfile
import threading
import readline
import traceback
from ctypes import (
    cdll, CDLL, RTLD_GLOBAL, byref, c_void_p, c_int, c_double, c_char_p,
    c_bool, create_string_buffer, addressof, POINTER
)
from ctypes.util import find_library
from datetime import datetime


class WaveformBuffer:
    """Thread-safe in-memory waveform accumulator fed by a FIFO reader.

    Xyce writes CSV to a FIFO. A reader thread parses rows into arrays.
    The main thread reads snapshots for display and periodic disk dumps.
    """

    def __init__(self):
        self.lock = threading.Lock()
        self.time = []           # all time values
        self.data = {}           # name -> [float, ...]
        self.names = []          # ordered column names (excluding time)
        self.header_parsed = False
        self._new_since_dump = 0  # rows added since last dump
        self._dump_offset = 0     # index of next row to dump

    def parse_csv_line(self, line):
        """Parse one CSV line. Called from reader thread."""
        line = line.strip()
        if not line:
            return

        with self.lock:
            if not self.header_parsed:
                cols = [c.strip().strip('"').strip('{}') for c in line.split(',')]
                self.names = cols[1:]  # skip TIME
                for col in self.names:
                    self.data[col] = []
                self.header_parsed = True
                return

            try:
                vals = [float(v.strip()) for v in line.split(',')]
            except ValueError:
                return

            if len(vals) < 1 + len(self.names):
                return

            self.time.append(vals[0])
            for i, col in enumerate(self.names):
                self.data[col].append(vals[i + 1])
            self._new_since_dump += 1

    def snapshot(self):
        """Get current data snapshot. Thread-safe."""
        with self.lock:
            return (list(self.time), dict(self.data), list(self.names))

    def point_count(self):
        with self.lock:
            return len(self.time)

    def last_values(self):
        """Get dict of name -> last value. Thread-safe."""
        with self.lock:
            result = {}
            for name in self.names:
                d = self.data.get(name, [])
                if d:
                    result[name] = d[-1]
            if self.time:
                result['TIME'] = self.time[-1]
            return result

    def get_dump_slice(self):
        """Get rows not yet dumped. Returns (time_slice, data_slices, names).
        Advances the dump offset."""
        with self.lock:
            start = self._dump_offset
            end = len(self.time)
            if start >= end:
                return None
            t_slice = self.time[start:end]
            d_slices = {}
            for name in self.names:
                d_slices[name] = self.data[name][start:end]
            self._dump_offset = end
            self._new_since_dump = 0
            return (t_slice, d_slices, list(self.names))

    def clear(self):
        with self.lock:
            self.time.clear()
            for d in self.data.values():
                d.clear()
            self.header_parsed = False
            self.names.clear()
            self.data.clear()
            self._new_since_dump = 0
            self._dump_offset = 0


class XyceInstance:
    """Wrapper around Xyce's C interface library."""

    def __init__(self, libdir=None):
        self.lib = None
        self.ptr = c_void_p()
        self.loaded = False
        self.initialized = False
        self.netlist = None
        self.sim_complete = False
        self.fifo_path = None     # named pipe for CSV data
        self.modified_netlist = None
        self.waveforms = WaveformBuffer()
        self._reader_thread = None
        self._reader_stop = threading.Event()

        # Legacy accessors (used by CLI commands)
        self.waveform_time = self.waveforms.time
        self.waveform_data = self.waveforms.data
        self.waveform_names = self.waveforms.names
        self.analysis_type = None

        self._load_lib(libdir)

    def _load_lib(self, libdir):
        """Find and load libxycecinterface.so"""
        search_paths = [
            libdir,
            os.environ.get('XYCE_LIB'),
            '/usr/local/XyceShared/lib',
            '/usr/local/XyceOpenSource/lib',
            '/usr/local/lib',
        ]

        # Try find_library first
        lib_name = find_library('xycecinterface')
        if lib_name:
            try:
                self.lib = CDLL(lib_name, RTLD_GLOBAL)
                self.loaded = True
                return
            except OSError:
                pass

        # Search known paths
        for path in search_paths:
            if not path:
                continue
            so_path = os.path.join(path, 'libxycecinterface.so')
            if os.path.exists(so_path):
                try:
                    self.lib = CDLL(so_path, RTLD_GLOBAL)
                    self.loaded = True
                    return
                except OSError as e:
                    print(f"Warning: found {so_path} but failed to load: {e}")

        raise OSError(
            "Could not find libxycecinterface.so. Build Xyce with "
            "BUILD_SHARED_LIBS=ON, or set XYCE_LIB to the lib directory."
        )

    def open(self):
        """Create a new Xyce instance."""
        self.lib.xyce_open(byref(self.ptr))

    def close(self):
        """Destroy the Xyce instance."""
        if self.ptr:
            self.lib.xyce_close(byref(self.ptr))
            self.ptr = c_void_p()
            self.initialized = False
            self.sim_complete = False
        self._stop_reader()
        for tmpf in (self.fifo_path, self.modified_netlist):
            if tmpf and os.path.exists(tmpf):
                try:
                    os.unlink(tmpf)
                except OSError:
                    pass
        self.fifo_path = None
        self.modified_netlist = None
        self.waveforms.clear()
        # Re-bind legacy accessors after clear
        self.waveform_time = self.waveforms.time
        self.waveform_data = self.waveforms.data
        self.waveform_names = self.waveforms.names

    def _start_reader(self):
        """Start background thread that reads CSV lines from the FIFO."""
        self._reader_stop.clear()
        self._reader_thread = threading.Thread(
            target=self._fifo_reader, daemon=True)
        self._reader_thread.start()

    def _fifo_reader(self):
        """Reader thread: opens FIFO (blocks until Xyce opens write end),
        then reads CSV lines into WaveformBuffer."""
        try:
            with open(self.fifo_path, 'r') as f:
                buf = ''
                while not self._reader_stop.is_set():
                    chunk = f.read(8192)
                    if not chunk:
                        break  # Xyce closed write end (sim done)
                    buf += chunk
                    while '\n' in buf:
                        line, buf = buf.split('\n', 1)
                        self.waveforms.parse_csv_line(line)
        except (IOError, OSError):
            pass

    def _stop_reader(self):
        """Stop the FIFO reader thread."""
        self._reader_stop.set()
        if self._reader_thread and self._reader_thread.is_alive():
            self._reader_thread.join(timeout=2.0)
        self._reader_thread = None

    def load_waveforms_from_raw(self):
        """Parse the Xyce-generated .raw file to populate waveform data."""
        if not self.raw_file or not os.path.exists(self.raw_file):
            return False
        if os.path.getsize(self.raw_file) < 100:
            return False

        try:
            return self._parse_raw_file(self.raw_file)
        except Exception:
            return False

    def _parse_raw_file(self, path):
        """Parse a SPICE .raw file (binary or ASCII)."""
        with open(path, 'rb') as f:
            raw = f.read()

        # Find header end
        header_end = raw.find(b'Binary:\n')
        is_binary = header_end >= 0
        if not is_binary:
            header_end = raw.find(b'Values:\n')
            if header_end < 0:
                return False

        header = raw[:header_end].decode('utf-8', errors='replace')

        # Parse header
        num_vars = 0
        num_points = 0
        is_complex = False
        var_names = []
        var_types = []
        in_variables = False

        for line in header.split('\n'):
            line = line.strip()
            if line.startswith('No. Variables:'):
                num_vars = int(line.split(':')[1].strip())
            elif line.startswith('No. Points:'):
                num_points = int(line.split(':')[1].strip())
            elif line.startswith('Flags:'):
                is_complex = 'complex' in line.lower()
            elif line == 'Variables:':
                in_variables = True
            elif in_variables and line and line[0].isdigit():
                parts = line.split('\t')
                if len(parts) >= 3:
                    var_names.append(parts[1])
                    var_types.append(parts[2])
            elif in_variables and not line:
                in_variables = False

        if not var_names or num_points == 0:
            return False

        # Parse data
        values_per_point = num_vars * (2 if is_complex else 1)

        if is_binary:
            data_start = header_end + len(b'Binary:\n')
            data = raw[data_start:]
            point_size = values_per_point * 8  # 8 bytes per double
            actual_points = len(data) // point_size
            num_points = min(num_points, actual_points)

            all_data = []
            for i in range(num_points):
                offset = i * point_size
                point = struct.unpack(f'{values_per_point}d',
                                     data[offset:offset + point_size])
                all_data.append(point)
        else:
            # ASCII format
            data_text = raw[header_end + len(b'Values:\n'):].decode('utf-8', errors='replace')
            all_data = []
            current_point = []
            for line in data_text.split('\n'):
                line = line.strip()
                if not line:
                    continue
                # Point index line: "0\t1.234, 0.0" or "0\t1.234"
                parts = line.split('\t')
                val_str = parts[-1]
                if ',' in val_str:
                    real, imag = val_str.split(',')
                    current_point.extend([float(real), float(imag)])
                else:
                    try:
                        current_point.append(float(val_str))
                    except ValueError:
                        continue
                if len(current_point) >= values_per_point:
                    all_data.append(tuple(current_point))
                    current_point = []

        # Populate waveform data
        self.waveform_time = []
        self.waveform_data = {}
        self.waveform_names = []

        step = 2 if is_complex else 1

        for i, name in enumerate(var_names):
            if i == 0:
                # Scale variable (time/frequency)
                self.waveform_time = [pt[0] for pt in all_data]
            else:
                col = i * step
                vname = f"V({name})" if var_types[i] == 'voltage' and '(' not in name else name
                if var_types[i] == 'current' and '(' not in vname:
                    vname = f"I({name})"
                self.waveform_names.append(vname)
                self.waveform_data[vname] = [pt[col] for pt in all_data]

        return True

    def _inject_csv_print(self, netlist_path):
        """Create a modified netlist with .PRINT FORMAT=CSV for live data.

        Scans the original netlist for .PRINT lines to determine which
        variables to track. Replaces them with a CSV .PRINT pointing at
        a named pipe (FIFO). A reader thread consumes the pipe into
        WaveformBuffer without touching disk.

        Returns path to the modified temp netlist.
        """
        # Create a named pipe (FIFO) — no disk I/O for waveform data
        self.fifo_path = tempfile.mktemp(suffix='.csv', prefix='xyce_fifo_')
        os.mkfifo(self.fifo_path)

        with open(netlist_path, 'r', errors='replace') as f:
            lines = f.readlines()

        # Find existing .PRINT lines and collect their variables
        print_vars = []
        print_indices = []
        for i, line in enumerate(lines):
            upper = line.strip().upper()
            if upper.startswith('.PRINT'):
                print_indices.append(i)
                # Extract variable names (skip .PRINT, analysis type, FORMAT=, FILE=)
                tokens = line.strip().split()
                for tok in tokens[2:]:  # skip .PRINT and TRAN/AC/DC
                    tok_up = tok.upper()
                    if tok_up.startswith('FORMAT=') or tok_up.startswith('FILE='):
                        continue
                    print_vars.append(tok)

        # If no .PRINT vars found, extract node names from device lines
        if not print_vars:
            nodes = extract_node_names(netlist_path)
            print_vars = [f'V({n})' for n in nodes]

        if not print_vars:
            print_vars = ['V(1)']  # fallback

        # Build CSV .PRINT line pointing at the FIFO
        analysis = self.analysis_type or 'TRAN'
        csv_print = (f'.PRINT {analysis.upper()} FORMAT=CSV '
                     f'FILE={self.fifo_path} {" ".join(print_vars)}\n')

        # Replace existing .PRINT lines or inject before .END
        new_lines = []
        replaced = False
        for i, line in enumerate(lines):
            if i in print_indices:
                if not replaced:
                    new_lines.append(csv_print)
                    replaced = True
                # Drop additional .PRINT lines (replace all with single CSV one)
            else:
                new_lines.append(line)

        if not replaced:
            # Insert before .END
            for i in range(len(new_lines) - 1, -1, -1):
                if new_lines[i].strip().upper() == '.END':
                    new_lines.insert(i, csv_print)
                    break
            else:
                new_lines.append(csv_print)

        fd, self.modified_netlist = tempfile.mkstemp(
            suffix='.cir', prefix='xyce_cli_')
        os.close(fd)
        with open(self.modified_netlist, 'w') as f:
            f.writelines(new_lines)

        return self.modified_netlist

    def read_csv_incremental(self):
        """Read new rows from the CSV file since last read.

        Returns number of new data points added.
        """
        if not self.csv_file or not os.path.exists(self.csv_file):
            return 0

        try:
            with open(self.csv_file, 'r') as f:
                f.seek(self.csv_pos)
                new_data = f.read()
                self.csv_pos = f.tell()
        except IOError:
            return 0

        if not new_data:
            return 0

        count = 0
        for line in new_data.split('\n'):
            line = line.strip()
            if not line:
                continue

            # First line is header
            if not self.csv_header:
                # Header: "time", "V(IN)", "V(OUT)", ...
                # Xyce CSV uses {name} format, strip braces and quotes
                cols = [c.strip().strip('"').strip('{}') for c in line.split(',')]
                self.csv_header = cols
                # Initialize waveform storage
                self.waveform_names = []
                self.waveform_data = {}
                for col in cols[1:]:  # skip time column
                    self.waveform_names.append(col)
                    self.waveform_data[col] = []
                continue

            # Data row
            try:
                vals = [float(v.strip()) for v in line.split(',')]
            except ValueError:
                continue

            if len(vals) < len(self.csv_header):
                continue

            self.waveform_time.append(vals[0])
            for i, col in enumerate(self.csv_header[1:], 1):
                if i < len(vals):
                    self.waveform_data[col].append(vals[i])

            count += 1

        return count

    def initialize(self, netlist_path, extra_args=None):
        """Initialize Xyce with a netlist file."""
        # Detect analysis type first (needed for CSV injection)
        self._detect_analysis(netlist_path)

        # Create modified netlist with CSV output for live data
        # Note: -r flag conflicts with FILE= in .PRINT, so we don't use -r
        modified = self._inject_csv_print(netlist_path)

        args = ['-quiet', '-l', os.devnull]
        if extra_args:
            args.extend(extra_args)
        args.append(modified)

        # Convert to C array
        args.insert(0, 'xyce_cli')
        narg = len(args)
        cargs = (c_char_p * narg)()
        for i, a in enumerate(args):
            cargs[i] = a.encode('utf-8')

        status = self.lib.xyce_initialize(byref(self.ptr), narg, cargs)
        if status == 1:
            self.initialized = True
            self.netlist = netlist_path  # store original path
            self.sim_complete = False
            self.csv_pos = 0
            self.csv_header = []
        return status

    def _detect_analysis(self, netlist_path):
        """Detect analysis type from netlist."""
        self.analysis_type = None
        try:
            with open(netlist_path, 'r', errors='replace') as f:
                for line in f:
                    upper = line.strip().upper()
                    if upper.startswith('.TRAN'):
                        self.analysis_type = 'tran'
                        break
                    elif upper.startswith('.AC'):
                        self.analysis_type = 'ac'
                        break
                    elif upper.startswith('.DC'):
                        self.analysis_type = 'dc'
                        break
        except IOError:
            pass

    def run_simulation(self):
        """Run simulation to completion."""
        status = self.lib.xyce_runSimulation(byref(self.ptr))
        self.sim_complete = True
        return status

    def simulate_until(self, requested_time):
        """Step simulation to a specific time. Returns (status, actual_time)."""
        actual = c_double(0)
        status = self.lib.xyce_simulateUntil(
            byref(self.ptr), c_double(requested_time), byref(actual)
        )
        return (status, actual.value)

    def simulation_complete(self):
        """Check if simulation has finished."""
        self.lib.xyce_simulationComplete.restype = c_bool
        return self.lib.xyce_simulationComplete(byref(self.ptr))

    def get_time(self):
        """Get current simulation time."""
        self.lib.xyce_getTime.restype = c_double
        return self.lib.xyce_getTime(byref(self.ptr))

    def get_final_time(self):
        """Get final simulation time from analysis statement."""
        self.lib.xyce_getFinalTime.restype = c_double
        return self.lib.xyce_getFinalTime(byref(self.ptr))

    def obtain_response(self, var_name):
        """Get current value of a circuit variable. Returns (status, value)."""
        cname = c_char_p(var_name.encode('utf-8'))
        cval = c_double(0.0)
        status = self.lib.xyce_obtainResponse(byref(self.ptr), cname, byref(cval))
        return (status, cval.value)

    def check_response_var(self, var_name):
        """Check if a response variable exists."""
        cname = c_char_p(var_name.encode('utf-8'))
        return self.lib.xyce_checkResponseVar(byref(self.ptr), cname)

    def get_circuit_value(self, param_name):
        """Get a circuit parameter value (e.g., R1:R, TEMP)."""
        cname = c_char_p(param_name.encode('utf-8'))
        self.lib.xyce_getCircuitValue.restype = c_double
        return self.lib.xyce_getCircuitValue(byref(self.ptr), cname)

    def set_circuit_parameter(self, param_name, value):
        """Set a circuit parameter value."""
        cname = c_char_p(param_name.encode('utf-8'))
        self.lib.xyce_setCircuitParameter.restype = c_int
        return self.lib.xyce_setCircuitParameter(
            byref(self.ptr), cname, c_double(value)
        )

    def check_circuit_parameter(self, param_name):
        """Check if a circuit parameter exists."""
        cname = c_char_p(param_name.encode('utf-8'))
        self.lib.xyce_checkCircuitParameterExists.restype = c_bool
        return self.lib.xyce_checkCircuitParameterExists(byref(self.ptr), cname)

    def get_num_devices(self, model_group):
        """Get number of devices of a given type."""
        cname = c_char_p(model_group.encode('utf-8'))
        cnum = c_int(0)
        cmax_len = c_int(0)
        status = self.lib.xyce_getNumDevices(
            byref(self.ptr), cname, byref(cnum), byref(cmax_len)
        )
        return (status, cnum.value, cmax_len.value)

    def get_device_names(self, model_group):
        """Get names of all devices of a given type."""
        cname = c_char_p(model_group.encode('utf-8'))
        cnum = c_int(0)
        cmax_len = c_int(0)

        status = self.lib.xyce_getNumDevices(
            byref(self.ptr), cname, byref(cnum), byref(cmax_len)
        )
        if status != 1 or cnum.value == 0:
            return (status, [])

        bufs = [create_string_buffer(cmax_len.value) for _ in range(cnum.value)]
        arr = (c_char_p * cnum.value)(*[addressof(b) for b in bufs])
        status = self.lib.xyce_getDeviceNames(
            byref(self.ptr), cname, byref(cnum), arr
        )
        names = [arr[i].decode('utf-8') for i in range(cnum.value)]
        return (status, names)

    def get_all_device_names(self):
        """Get names of ALL devices in the circuit."""
        cnum = c_int(0)
        cmax_len = c_int(0)

        status = self.lib.xyce_getTotalNumDevices(
            byref(self.ptr), byref(cnum), byref(cmax_len)
        )
        if status != 1 or cnum.value == 0:
            return (status, [])

        bufs = [create_string_buffer(cmax_len.value) for _ in range(cnum.value)]
        arr = (c_char_p * cnum.value)(*[addressof(b) for b in bufs])
        status = self.lib.xyce_getAllDeviceNames(
            byref(self.ptr), byref(cnum), arr
        )
        names = [arr[i].decode('utf-8') for i in range(cnum.value)]
        return (status, names)

    def get_device_param(self, full_param_name):
        """Get a device parameter value (e.g., R1:R)."""
        cname = c_char_p(full_param_name.encode('utf-8'))
        cval = c_double(0.0)
        status = self.lib.xyce_getDeviceParamVal(
            byref(self.ptr), cname, byref(cval)
        )
        return (status, cval.value)


def parse_eng(s):
    """Parse engineering notation: 1k -> 1000, 10u -> 1e-5, etc."""
    suffixes = {
        'T': 1e12, 'G': 1e9, 'MEG': 1e6, 'K': 1e3,
        'M': 1e-3, 'U': 1e-6, 'N': 1e-9, 'P': 1e-12, 'F': 1e-15,
    }
    s = s.strip().upper()
    for suf, mult in sorted(suffixes.items(), key=lambda x: -len(x[0])):
        if s.endswith(suf):
            num = s[:-len(suf)]
            try:
                return float(num) * mult
            except ValueError:
                pass
    return float(s)


def extract_node_names(netlist_path):
    """Extract node names from a netlist by parsing device lines."""
    nodes = set()
    try:
        with open(netlist_path, 'r', errors='replace') as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith('*') or line.startswith('.'):
                    continue
                # Device lines: PREFIX<name> node1 node2 ... value
                parts = line.split()
                if len(parts) < 3:
                    continue
                prefix = parts[0][0].upper()
                if prefix in 'RCLVIDEQMJKXBGFSTHUW':
                    # Nodes are the middle fields (not first=name, not last=value/model)
                    # For 2-terminal: parts[1], parts[2]
                    # Heuristic: nodes are non-numeric, non-model strings
                    for p in parts[1:]:
                        # Stop at values (numbers, model names with params)
                        if re.match(r'^[+-]?\d', p) and not re.match(r'^\d+$', p):
                            break
                        if p == '0' or re.match(r'^[A-Za-z_]\w*$', p) or re.match(r'^\d+$', p):
                            nodes.add(p)
                        else:
                            break
    except IOError:
        pass
    # Remove '0' as it's ground
    nodes.discard('0')
    return sorted(nodes)


def write_raw_file(path, title, analysis_type, time_data, waveform_data,
                   waveform_names, ascii_mode=False):
    """Write simulation data in LTspice-compatible .raw format."""
    is_complex = (analysis_type == 'ac')
    num_vars = 1 + len(waveform_names)  # time/freq + signals
    num_points = len(time_data)

    # Determine scale variable name and type
    if analysis_type == 'ac':
        scale_name = 'frequency'
        scale_type = 'frequency'
        plot_name = 'AC Analysis'
    elif analysis_type == 'dc':
        scale_name = 'sweep'
        scale_type = 'voltage'
        plot_name = 'DC Analysis'
    else:
        scale_name = 'time'
        scale_type = 'time'
        plot_name = 'Transient Analysis'

    with open(path, 'wb' if not ascii_mode else 'w',
              **(dict(newline='') if ascii_mode else {})) as f:
        def wh(s):
            if ascii_mode:
                f.write(s)
            else:
                f.write(s.encode('utf-8'))

        wh(f"Title: {title}\n")
        wh(f"Date: {datetime.now().strftime('%a %b %d %H:%M:%S %Y')}\n")
        wh(f"Plotname: {plot_name}\n")
        wh(f"Flags: {'complex' if is_complex else 'real'}\n")
        wh(f"No. Variables: {num_vars}\n")
        wh(f"No. Points: {num_points}\n")
        wh("Variables:\n")
        wh(f"\t0\t{scale_name}\t{scale_type}\n")
        for i, name in enumerate(waveform_names):
            vtype = 'current' if name.startswith('I(') else 'voltage'
            wh(f"\t{i+1}\t{name}\t{vtype}\n")

        if ascii_mode:
            wh("Values:\n")
            for pt in range(num_points):
                if is_complex:
                    wh(f"{pt}\t{time_data[pt]}, 0.0\n")
                else:
                    wh(f"{pt}\t{time_data[pt]}\n")
                for name in waveform_names:
                    val = waveform_data.get(name, [0.0] * num_points)[pt]
                    if is_complex:
                        wh(f"\t{val}, 0.0\n")
                    else:
                        wh(f"\t{val}\n")
                wh("\n")
        else:
            wh("Binary:\n")
            for pt in range(num_points):
                f.write(struct.pack('d', time_data[pt]))
                if is_complex:
                    f.write(struct.pack('d', 0.0))  # imaginary part of freq
                for name in waveform_names:
                    val = waveform_data.get(name, [0.0] * num_points)[pt]
                    f.write(struct.pack('d', val))
                    if is_complex:
                        f.write(struct.pack('d', 0.0))


class WaveformViewer:
    """PyQtGraph-based waveform viewer.

    Qt must run in the main thread. When plot is requested, we:
    1. Move the REPL to a background thread
    2. Run Qt's event loop in the main thread
    3. Use a QTimer to check for REPL commands
    """

    def __init__(self):
        self.app = None
        self.win = None
        self.plots = {}      # var_name -> PlotDataItem
        self.plot_widget = None
        self.running = False
        self.pg = None

    def init_qt(self):
        """Initialize Qt and PyQtGraph (must be called from main thread)."""
        if self.running:
            return

        import pyqtgraph as pg
        from PyQt5 import QtWidgets
        self.pg = pg

        self.app = QtWidgets.QApplication.instance()
        if not self.app:
            self.app = QtWidgets.QApplication([])

        pg.setConfigOptions(antialias=True, background='w', foreground='k')

        self.win = pg.GraphicsLayoutWidget(title='ltz Waveform Viewer')
        self.win.resize(1000, 600)
        self.win.show()

        self.plot_widget = self.win.addPlot()
        self.plot_widget.showGrid(x=True, y=True, alpha=0.3)
        self.plot_widget.addLegend()
        self.plot_widget.setLabel('bottom', 'Time', units='s')
        self.plot_widget.setLabel('left', 'Value')

        self.running = True

    def update(self, time_data, waveform_data, var_names):
        """Update plot data (must be called from main thread)."""
        if not self.running:
            return

        import numpy as np

        colors = [
            (31, 119, 180),   # blue
            (255, 127, 14),   # orange
            (44, 160, 44),    # green
            (214, 39, 40),    # red
            (148, 103, 189),  # purple
            (140, 86, 75),    # brown
            (227, 119, 194),  # pink
            (127, 127, 127),  # gray
        ]

        if not time_data:
            return

        t = np.array(time_data)

        for i, name in enumerate(var_names):
            data = waveform_data.get(name, [])
            if not data:
                continue
            y = np.array(data)
            color = colors[i % len(colors)]

            if name in self.plots:
                self.plots[name].setData(t[:len(y)], y)
            else:
                pen = self.pg.mkPen(color=color, width=2)
                curve = self.plot_widget.plot(t[:len(y)], y, pen=pen, name=name)
                self.plots[name] = curve

        self.app.processEvents()

    def clear(self):
        """Clear all plot curves."""
        if not self.running:
            return
        for curve in self.plots.values():
            self.plot_widget.removeItem(curve)
        self.plots.clear()
        if self.plot_widget.legend:
            self.plot_widget.legend.clear()
        self.app.processEvents()


class XyceCLI:
    """Interactive REPL for Xyce simulation."""

    def __init__(self, libdir=None):
        self.xyce = None
        self.libdir = libdir
        self.step_count = 0
        self.default_step = None
        self.viewer = None
        self.plot_vars = []  # variables selected for plotting

    def start(self, initial_netlist=None):
        """Start the REPL."""
        print("Xyce Interactive CLI (ltz)")
        print("Type 'help' for commands, 'quit' to exit.\n")

        try:
            self.xyce = XyceInstance(self.libdir)
            print(f"Loaded Xyce library successfully.")
        except OSError as e:
            print(f"Error: {e}")
            return 1

        if initial_netlist:
            self._cmd_source(initial_netlist)

        self._repl()
        return 0

    def _repl(self):
        """Main read-eval-print loop."""
        # Set up readline history
        histfile = os.path.expanduser('~/.xyce_cli_history')
        try:
            readline.read_history_file(histfile)
        except FileNotFoundError:
            pass

        while True:
            try:
                prompt = "xyce> " if not self.xyce.initialized else \
                    f"xyce [{self.xyce.analysis_type or '?'}]> "
                line = input(prompt).strip()
            except (EOFError, KeyboardInterrupt):
                print()
                break

            if not line:
                continue

            try:
                self._dispatch(line)
            except SystemExit:
                break
            except Exception as e:
                print(f"Error: {e}")
                if os.environ.get('XYCE_CLI_DEBUG'):
                    traceback.print_exc()

            # Keep Qt responsive between commands
            if self.viewer and self.viewer.running and self.viewer.app:
                self.viewer.app.processEvents()

        # Save history
        try:
            readline.write_history_file(histfile)
        except IOError:
            pass

        self._cleanup()

    def _dispatch(self, line):
        """Parse and dispatch a command."""
        parts = line.split(None, 1)
        cmd = parts[0].lower()
        args = parts[1] if len(parts) > 1 else ''

        commands = {
            'source': self._cmd_source,
            'run': lambda a=None: self._cmd_run(),
            'step': self._cmd_step,
            'print': self._cmd_print,
            'show': lambda a=None: self._cmd_show(),
            'plot': self._cmd_plot,
            'alter': self._cmd_alter,
            'status': lambda a=None: self._cmd_status(),
            'reset': lambda a=None: self._cmd_reset(),
            'devices': self._cmd_devices,
            'param': self._cmd_param,
            'write': self._cmd_write,
            'help': lambda a=None: self._cmd_help(),
            'quit': lambda a=None: self._cmd_quit(),
            'exit': lambda a=None: self._cmd_quit(),
        }

        handler = commands.get(cmd)
        if handler:
            handler(args)
        else:
            print(f"Unknown command: '{cmd}'. Type 'help' for available commands.")

    def _require_init(self):
        """Check that a netlist is loaded."""
        if not self.xyce or not self.xyce.initialized:
            print("No netlist loaded. Use 'source <file>' first.")
            return False
        return True

    def _cmd_source(self, path):
        """Load a netlist."""
        path = path.strip().strip('"').strip("'")
        if not path:
            if self.xyce.netlist:
                path = self.xyce.netlist
                print(f"Reloading: {path}")
            else:
                print("Usage: source <netlist.cir>")
                return

        if not os.path.isfile(path):
            print(f"File not found: {path}")
            return

        # Close existing instance and reopen
        if self.xyce.initialized:
            self.xyce.close()

        self.xyce.open()
        status = self.xyce.initialize(os.path.abspath(path))
        if status == 1:
            self.step_count = 0
            self.xyce.waveform_time = []
            self.xyce.waveform_data = {}
            self.xyce.waveform_names = []

            final_t = self.xyce.get_final_time()
            self.default_step = final_t / 100 if final_t > 0 else 1e-6

            # Get device list for context
            (_, all_devs) = self.xyce.get_all_device_names()

            print(f"Loaded: {path}")
            print(f"  Analysis: {self.xyce.analysis_type or 'unknown'}")
            print(f"  Final time: {final_t:.6g}")
            print(f"  Devices: {len(all_devs)}")
        else:
            print(f"Failed to initialize netlist (status={status})")

    def _cmd_run(self):
        """Run simulation to completion, with live waveform updates."""
        if not self._require_init():
            return

        if self.xyce.sim_complete:
            print("Simulation already complete. Use 'reset' to restart.")
            return

        t_start = time.time()
        final_t = self.xyce.get_final_time()
        viewer_active = self.viewer and self.viewer.running

        if viewer_active and final_t > 0:
            # Step-based run for live viewer updates
            print("Running simulation (live)...")
            step_dt = final_t / 100
            while not self.xyce.sim_complete:
                cur = self.xyce.get_time()
                target = min(cur + step_dt, final_t * 1.1)
                (status, actual) = self.xyce.simulate_until(target)
                self.step_count += 1
                self.xyce.read_csv_incremental()
                self._update_viewer()

                if self.xyce.simulation_complete():
                    self.xyce.sim_complete = True
                    self.xyce.read_csv_incremental()
                    self._update_viewer()
                    break
        else:
            # Fast path: run to completion, parse CSV at end
            print("Running simulation...")
            self.xyce.run_simulation()
            self.xyce.sim_complete = True
            self.xyce.read_csv_incremental()

        elapsed = time.time() - t_start
        n_pts = len(self.xyce.waveform_time)
        n_vars = len(self.xyce.waveform_names)
        print(f"Simulation complete. {n_pts} points, {n_vars} variables ({elapsed:.3f}s)")
        if self.xyce.waveform_names:
            print(f"  Variables: {', '.join(self.xyce.waveform_names[:10])}"
                  + ("..." if n_vars > 10 else ""))
        self._update_viewer()

    def _cmd_step(self, args):
        """Step simulation forward by a time delta."""
        if not self._require_init():
            return

        if self.xyce.sim_complete:
            print("Simulation already complete. Use 'reset' to restart.")
            return

        if args.strip():
            try:
                dt = parse_eng(args.strip())
            except ValueError:
                print(f"Invalid time value: {args}")
                return
        else:
            dt = self.default_step

        cur_time = self.xyce.get_time()
        target = cur_time + dt
        (status, actual) = self.xyce.simulate_until(target)

        if status == 1 or status == 2:
            self.step_count += 1

            # Read new CSV data points (live)
            new_pts = self.xyce.read_csv_incremental()

            if self.xyce.simulation_complete():
                self.xyce.sim_complete = True
                # Final flush — read any remaining CSV data
                self.xyce.read_csv_incremental()
                print(f"Step to t={actual:.6g} — simulation complete "
                      f"({len(self.xyce.waveform_time)} pts)")
            else:
                print(f"Step to t={actual:.6g} (+{new_pts} pts, "
                      f"{len(self.xyce.waveform_time)} total)")

            self._update_viewer()
        else:
            print(f"Step failed (status={status})")

    def _cmd_print(self, args):
        """Print current value of a variable."""
        if not self._require_init():
            return

        var = args.strip()
        if not var:
            print("Usage: print V(node) | I(device) | param_name")
            return

        # Read any pending CSV data first
        self.xyce.read_csv_incremental()

        # Try waveform data (last value) — check exact, V()-wrapped, uppercase
        for candidate in (var, f"V({var})", var.upper(), f"V({var.upper()})"):
            if candidate in self.xyce.waveform_data and self.xyce.waveform_data[candidate]:
                val = self.xyce.waveform_data[candidate][-1]
                t = self.xyce.waveform_time[-1] if self.xyce.waveform_time else 0
                print(f"  {candidate} = {val:+.6g}  (t={t:.6g})")
                return

        # Try as circuit parameter (device params like R1:R)
        if self.xyce.check_circuit_parameter(var):
            val = self.xyce.get_circuit_value(var)
            print(f"  {var} = {val:.6g}")
            return

        print(f"  Variable '{var}' not found. Step/run simulation first, or try V(node) / dev:param")

    def _cmd_show(self):
        """Show all waveform variables at last time point, or device params."""
        if not self._require_init():
            return

        # Try reading any new CSV data first
        self.xyce.read_csv_incremental()

        t = self.xyce.get_time()
        print(f"  time = {t:.6g}")

        if self.xyce.waveform_names:
            for var in self.xyce.waveform_names:
                data = self.xyce.waveform_data.get(var, [])
                if data:
                    print(f"  {var:20s} = {data[-1]:+.6g}")
        else:
            # No waveform data yet — show device params
            print("  (waveform data available after stepping/running)")
            (_, devs) = self.xyce.get_all_device_names()
            for d in devs:
                prefix = d[0].upper() if d else ''
                param_map = {'R': ':R', 'C': ':C', 'L': ':L', 'V': ':DCV0'}
                suffix = param_map.get(prefix)
                if suffix:
                    pname = d + suffix
                    if self.xyce.check_circuit_parameter(pname):
                        val = self.xyce.get_circuit_value(pname)
                        print(f"  {pname:20s} = {val:+.6g}")

    def _cmd_plot(self, args):
        """Open waveform viewer and plot variables."""
        if not self._require_init():
            return

        # Parse variable names from args
        if args.strip():
            requested = [v.strip() for v in args.strip().split()]
            var_names = []
            for req in requested:
                # Try exact match, V()-wrapped, and uppercase variants
                for candidate in (req, f"V({req})", req.upper(), f"V({req.upper()})"):
                    if candidate in self.xyce.waveform_data:
                        var_names.append(candidate)
                        break
                else:
                    # Accept anyway — data may arrive later during stepping
                    var_names.append(req)
        else:
            # Plot all voltage variables by default
            var_names = [v for v in self.xyce.waveform_names if v.startswith('V(')]
            if not var_names:
                var_names = self.xyce.waveform_names[:]
            if not var_names:
                # No data yet — will populate when CSV header is read
                var_names = []

        self.plot_vars = var_names

        # Start viewer if not running
        if not self.viewer or not self.viewer.running:
            try:
                self.viewer = WaveformViewer()
                self.viewer.init_qt()
            except Exception as e:
                print(f"Failed to start viewer: {e}")
                return

        self._update_viewer()
        if var_names:
            print(f"Plotting: {', '.join(var_names)}")
        else:
            print("Viewer ready. Variables will appear after stepping/running.")

    def _update_viewer(self):
        """Push current waveform data to the viewer."""
        if not self.viewer or not self.viewer.running:
            return
        if not self.xyce.waveform_time:
            return

        var_names = self.plot_vars if self.plot_vars else self.xyce.waveform_names
        # Filter to only variables that have data
        var_names = [v for v in var_names if v in self.xyce.waveform_data]

        # Auto-populate plot_vars if they were empty and data arrived
        if not self.plot_vars and var_names:
            self.plot_vars = [v for v in var_names if v.startswith('V(')]
            if not self.plot_vars:
                self.plot_vars = var_names[:]
            var_names = self.plot_vars

        self.viewer.update(
            self.xyce.waveform_time,
            self.xyce.waveform_data,
            var_names,
        )

    def _cmd_alter(self, args):
        """Change a device parameter."""
        if not self._require_init():
            return

        parts = args.strip().split()
        if len(parts) < 2:
            print("Usage: alter <device:param> <value>")
            print("   or: alter <device> <value>  (changes primary param)")
            return

        param_name = parts[0]
        try:
            value = parse_eng(parts[1])
        except ValueError:
            print(f"Invalid value: {parts[1]}")
            return

        # If no colon, guess the primary parameter
        if ':' not in param_name:
            prefix = param_name[0].upper()
            param_map = {'R': ':R', 'C': ':C', 'L': ':L'}
            param_name += param_map.get(prefix, ':R')

        status = self.xyce.set_circuit_parameter(param_name, value)
        if status:
            print(f"  {param_name} = {value:.6g}")
        else:
            print(f"  Failed to set '{param_name}'")

    def _cmd_status(self):
        """Show simulation status."""
        if not self._require_init():
            return

        t = self.xyce.get_time()
        t_final = self.xyce.get_final_time()
        complete = self.xyce.simulation_complete()
        pct = (t / t_final * 100) if t_final > 0 else 0

        print(f"  Netlist:    {self.xyce.netlist}")
        print(f"  Analysis:   {self.xyce.analysis_type or 'unknown'}")
        print(f"  Time:       {t:.6g} / {t_final:.6g}  ({pct:.1f}%)")
        print(f"  Complete:   {complete}")
        print(f"  Steps:      {self.step_count}")
        print(f"  Data pts:   {len(self.xyce.waveform_time)}")
        print(f"  Variables:  {len(self.xyce.waveform_names)}")

    def _cmd_reset(self):
        """Reset simulation (close and reopen with same netlist)."""
        if not self.xyce or not self.xyce.netlist:
            print("No netlist loaded.")
            return

        path = self.xyce.netlist
        print(f"Resetting simulation...")
        self._cmd_source(path)

    def _cmd_devices(self, args):
        """List devices in the circuit."""
        if not self._require_init():
            return

        filter_type = args.strip().upper() if args.strip() else None

        if filter_type:
            (status, names) = self.xyce.get_device_names(filter_type)
            if status == 1 and names:
                for n in names:
                    print(f"  {n}")
            else:
                print(f"  No devices of type '{filter_type}'")
        else:
            (status, names) = self.xyce.get_all_device_names()
            if status == 1 and names:
                # Group by prefix
                groups = {}
                for n in names:
                    prefix = n[0] if n else '?'
                    groups.setdefault(prefix, []).append(n)
                for prefix in sorted(groups):
                    devs = groups[prefix]
                    print(f"  {prefix}: {', '.join(devs)}")
            else:
                print("  No devices found")

    def _cmd_param(self, args):
        """Query a device parameter."""
        if not self._require_init():
            return

        param = args.strip()
        if not param:
            print("Usage: param <device:param>  (e.g., param R1:R)")
            return

        (status, val) = self.xyce.get_device_param(param)
        if status == 1:
            print(f"  {param} = {val:.6g}")
        else:
            print(f"  Parameter '{param}' not found")

    def _cmd_write(self, args):
        """Write waveform data to .raw file."""
        if not self.xyce or not self.xyce.waveform_time:
            print("No waveform data to write. Run a simulation first.")
            return

        path = args.strip() or (
            os.path.splitext(self.xyce.netlist)[0] + '.raw'
            if self.xyce.netlist else 'output.raw'
        )

        title = os.path.basename(self.xyce.netlist) if self.xyce.netlist else 'untitled'

        write_raw_file(
            path=path,
            title=title,
            analysis_type=self.xyce.analysis_type or 'tran',
            time_data=self.xyce.waveform_time,
            waveform_data=self.xyce.waveform_data,
            waveform_names=self.xyce.waveform_names,
        )
        n = len(self.xyce.waveform_time)
        v = len(self.xyce.waveform_names)
        print(f"Wrote {path} ({n} points, {v} variables)")

    def _cmd_help(self):
        """Show help text."""
        print("""Commands:
  source <file>       Load a netlist (or reload current)
  run                 Run simulation to completion
  step [time]         Advance simulation by time delta
  print <expr>        Show value of V(node), I(device), or parameter
  show                List all node voltages at current time
  plot [vars...]      Open waveform viewer (e.g., plot V(OUT) V(IN))
  alter <dev> <val>   Change device parameter (e.g., alter R1 1k)
  status              Show simulation time, progress, state
  reset               Restart simulation from t=0
  devices [type]      List all devices (optionally filter by prefix)
  param <dev:param>   Query device parameter value
  write [file.raw]    Write waveform data to .raw file
  help                Show this help
  quit / exit         Exit""")

    def _cmd_quit(self):
        """Exit the CLI."""
        self._cleanup()
        raise SystemExit(0)

    def _cleanup(self):
        """Clean shutdown."""
        if self.xyce and self.xyce.initialized:
            try:
                self.xyce.close()
            except Exception:
                pass


def main():
    import argparse
    parser = argparse.ArgumentParser(
        description='Interactive CLI for Xyce circuit simulation'
    )
    parser.add_argument('netlist', nargs='?', help='Initial netlist file')
    parser.add_argument('--lib', help='Path to Xyce shared library directory')
    args = parser.parse_args()

    cli = XyceCLI(libdir=args.lib)
    sys.exit(cli.start(initial_netlist=args.netlist))


if __name__ == '__main__':
    main()
