# scope_gui.py
import threading
import time
import struct
import tkinter as tk
from tkinter import ttk, messagebox

from lib.MeasurementDevice import MeasurementDevice, MeasurementDeviceError

import matplotlib
matplotlib.use("TkAgg")
from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg
from matplotlib.figure import Figure


# ============================================================================
# Scope sample decoding + current conversion for DT pressure/current front-end
#
# AVERAGE payload:
#   uint16 per sample (big-endian):
#     bits 0..11  -> raw12 value (0..4095)
#     bits 12..15 -> marker flags (0..15)
#
# For plotting/metrics we ignore samples with marks != 0 (trigger/marker events).
#
# RAW(12-bit) -> Voltage (offset-binary around 2048):
#   V = (raw12 - 2048) / 2048.0 * V_FS
#
# Voltage -> Current:
#   I_mA = (V / R_SHUNT) * 1000
#
# Current -> Pressure:
#   P_bar = I_mA * PRESSURE_FACTOR
#
# Calibration (firmware semantics, important):
#   - Gains are ppm-like: gainFactor = 1 + gain/100000
#   - Offsets are also ppm-like and applied as fraction of full-range:
#       V_corr = V * fullGain + (fullOffset_ppm / 100000) * V_FS
#
# Composition:
#   fullGain       = (1 + intGain/100000) * (1 + extGain/100000)
#   fullOffset_ppm = fullGain * intOffset + extOffset
#
# Where:
#   extOffset = frame["CalOffset"]
#   extGain   = frame["CalGain"]
#   intOffset = frame["CalcOffsetScopeChannel"]
#   intGain   = frame["CalcGainScopeChannel"]
# ============================================================================

V_FS_DEFAULT = 1.0
R_SHUNT_DEFAULT = 49.9
PRESSURE_FACTOR_DEFAULT = 1.0
GAIN_DIV = 100000.0


def raw12_to_voltage(raw12: int, v_fs: float) -> float:
    """Convert 12-bit offset-binary code to volts."""
    return ((float(raw12) - 2048.0) / 2048.0) * float(v_fs)


def voltage_to_mA(v: float, r_shunt: float) -> float:
    """Convert volts across shunt to mA."""
    return (float(v) / float(r_shunt)) * 1000.0


def current_to_pressure_bar(i_ma: float, factor: float) -> float:
    """Convert current in mA to pressure in bar."""
    return float(i_ma) * float(factor)


def compute_full_gain_and_offset_ppm(frame: dict) -> tuple[float, float]:
    """
    Compute fullGain and fullOffset_ppm per firmware semantics.

    fullGain       = (1 + intGain/1e5) * (1 + extGain/1e5)
    fullOffset_ppm = fullGain * intOffset + extOffset
    """
    ext_gain = float(frame.get("CalGain", 0))
    ext_off = float(frame.get("CalOffset", 0))
    int_gain = float(frame.get("CalcGainScopeChannel", 0))
    int_off = float(frame.get("CalcOffsetScopeChannel", 0))

    full_gain = (1.0 + int_gain / GAIN_DIV) * (1.0 + ext_gain / GAIN_DIV)
    full_offset_ppm = (full_gain * int_off) + ext_off
    return full_gain, full_offset_ppm


def apply_calibration_to_voltage(v: float, v_fs: float, full_gain: float, full_offset_ppm: float) -> float:
    """
    Apply calibration to a voltage-like quantity, matching the firmware's usage:

      v_corr = v * fullGain + (fullOffset_ppm / 1e5) * full_range

    Here we use full_range = V_FS (effective full-scale volts used in raw12_to_voltage).
    """
    return (v * full_gain) + ((full_offset_ppm / GAIN_DIV) * float(v_fs))


def decode_scope_samples(frame: dict) -> dict:
    """
    Decode scope samples from one received frame.

    Returns:
      {
        "mode": str,
        "raw12": list[int],          # value bits only (12-bit), for chosen view
        "marks": list[int] | None,   # marker flags (AVERAGE only)
        "pairs": list[tuple[int,int]] | None,  # for 32-bit modes: (low16, high16)
      }
    """
    data: bytes = frame["Data"]
    sample_method_id = frame["SampleMethod"]

    method_name = None
    try:
        for k, v in MeasurementDevice.scope_sample_methods.items():
            if v == sample_method_id:
                method_name = k
                break
    except Exception:
        method_name = None
    mode = method_name or f"UNKNOWN({sample_method_id})"

    if sample_method_id == MeasurementDevice.scope_sample_methods.get("AVERAGE"):
        n = len(data) // 2
        raw12: list[int] = []
        marks: list[int] = []
        for i in range(n):
            (u16,) = struct.unpack(">H", data[i * 2:(i + 1) * 2])
            raw12.append(u16 & 0x0FFF)
            marks.append((u16 >> 12) & 0x000F)
        return {"mode": mode, "raw12": raw12, "marks": marks, "pairs": None}

    n = len(data) // 4
    pairs: list[tuple[int, int]] = []
    for i in range(n):
        (u32,) = struct.unpack(">I", data[i * 4:(i + 1) * 4])
        low16 = u32 & 0xFFFF
        high16 = (u32 >> 16) & 0xFFFF
        pairs.append((low16, high16))

    raw12_view: list[int] = []
    if method_name == "MINMAX":
        for low16, high16 in pairs:
            raw12_view.append(((low16 & 0x0FFF) + (high16 & 0x0FFF)) // 2)
    elif method_name == "MIN":
        for low16, _high16 in pairs:
            raw12_view.append(low16 & 0x0FFF)
    elif method_name == "MAX":
        for _low16, high16 in pairs:
            raw12_view.append(high16 & 0x0FFF)
    else:
        for low16, _high16 in pairs:
            raw12_view.append(low16 & 0x0FFF)

    return {"mode": mode, "raw12": raw12_view, "marks": None, "pairs": pairs}


def filter_valid_by_marks(raw12: list[int], marks: list[int] | None) -> list[int]:
    """Return only samples with marks==0 (or all samples if marks is None)."""
    if marks is None:
        return raw12
    out: list[int] = []
    for v, m in zip(raw12, marks):
        if m == 0:
            out.append(v)
    return out


class ScopeGUI(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("WaveScope GUI (DT Pressure Sensor 0x08)")
        self.geometry("1150x780")

        self.vas: MeasurementDevice | None = None
        self.reader_thread: threading.Thread | None = None
        self.stop_event = threading.Event()

        # ---- UI state -------------------------------------------------------
        self.ip_var = tk.StringVar(value="192.168.111.111")
        self.port_var = tk.StringVar(value="55555")

        self.channel_var = tk.StringVar(value="A")
        self.range_var = tk.StringVar(value="NO")
        self.coupling_var = tk.StringVar(value="DC")
        self.mode_var = tk.StringVar(value="MANUAL")

        self.filter_var = tk.StringVar(value="1MHz")

        self.sample_rate_var = tk.StringVar(value="10KS")
        self.sample_mode_var = tk.StringVar(value="AVERAGE")
        self.count_var = tk.StringVar(value="1000")

        # Display/Conversion controls
        self.display_mode_var = tk.StringVar(value="mA")  # RAW12 / mA / bar
        self.vfs_var = tk.StringVar(value=str(V_FS_DEFAULT))
        self.rshunt_var = tk.StringVar(value=str(R_SHUNT_DEFAULT))
        self.pressure_factor_var = tk.StringVar(value=str(PRESSURE_FACTOR_DEFAULT))

        # Axis scaling
        self.fixed_axis_var = tk.BooleanVar(value=True)
        self.xmin_var = tk.StringVar(value="0")
        self.xmax_var = tk.StringVar(value="1000")
        self.ymin_var = tk.StringVar(value="0")
        self.ymax_var = tk.StringVar(value="25")

        # Min/Max display
        self.min_value_var = tk.StringVar(value="---")
        self.max_value_var = tk.StringVar(value="---")
        self.minmax_unit_var = tk.StringVar(value="mA")
        self.global_min_value: float | None = None
        self.global_max_value: float | None = None

        # Detected socket display
        self.detected_socket_var = tk.StringVar(value="(not connected)")
        self.detected_dt1_var = tk.StringVar(value="----")
        self.detected_dt2_var = tk.StringVar(value="----")

        self._build_ui()
        self.protocol("WM_DELETE_WINDOW", self.on_close)

        self._init_plot()

        self.latest_y: list[float] | None = None
        self.after(30, self._update_plot_loop)

        # Throttle debug output
        self._last_debug_t = 0.0
        self._debug_interval_s = 1.0

    # --------------------------------------------------------------------- UI
    def _build_ui(self):
        outer = ttk.Frame(self, padding=10)
        outer.pack(fill="both", expand=True)

        top = ttk.Frame(outer)
        top.pack(fill="x")

        # Connection frame
        conn = ttk.LabelFrame(top, text="Connection", padding=10)
        conn.pack(side="left", fill="x", expand=True, padx=(0, 10))

        ttk.Label(conn, text="IP:").grid(row=0, column=0, sticky="w")
        ttk.Entry(conn, textvariable=self.ip_var, width=18).grid(row=0, column=1, sticky="w", padx=5)

        ttk.Label(conn, text="Port:").grid(row=0, column=2, sticky="w")
        ttk.Entry(conn, textvariable=self.port_var, width=8).grid(row=0, column=3, sticky="w", padx=5)

        ttk.Button(conn, text="Connect", command=self.connect).grid(row=0, column=4, sticky="w", padx=10)
        ttk.Button(conn, text="Disconnect", command=self.disconnect).grid(row=0, column=5, sticky="w")

        ttk.Label(conn, text="Detected socket (0x08):").grid(row=1, column=0, sticky="w", pady=(10, 0))
        ttk.Label(conn, textvariable=self.detected_socket_var).grid(row=1, column=1, columnspan=5, sticky="w", pady=(10, 0))

        ttk.Label(conn, text="DT_1:").grid(row=2, column=0, sticky="w")
        ttk.Label(conn, textvariable=self.detected_dt1_var).grid(row=2, column=1, sticky="w")
        ttk.Label(conn, text="DT_2:").grid(row=2, column=2, sticky="w")
        ttk.Label(conn, textvariable=self.detected_dt2_var).grid(row=2, column=3, sticky="w")

        # Control frame
        ctrl = ttk.LabelFrame(top, text="Scope Settings", padding=10)
        ctrl.pack(side="left", fill="x", expand=True)

        ttk.Label(ctrl, text="Filter:").grid(row=0, column=0, sticky="w")
        filters = list(MeasurementDevice.scope_filters.keys())
        ttk.Combobox(ctrl, textvariable=self.filter_var, values=filters, state="readonly", width=12).grid(
            row=0, column=1, sticky="w", padx=5
        )

        ttk.Label(ctrl, text="SampleRate:").grid(row=0, column=2, sticky="w")
        rates = list(MeasurementDevice.scope_sample_rates.keys())
        ttk.Combobox(ctrl, textvariable=self.sample_rate_var, values=rates, state="readonly", width=12).grid(
            row=0, column=3, sticky="w", padx=5
        )

        ttk.Label(ctrl, text="Count:").grid(row=0, column=4, sticky="w")
        ttk.Entry(ctrl, textvariable=self.count_var, width=8).grid(row=0, column=5, sticky="w", padx=5)

        mode_box = ttk.LabelFrame(ctrl, text="SampleMode", padding=8)
        mode_box.grid(row=1, column=0, columnspan=6, sticky="we", pady=(10, 0))
        for i, name in enumerate(["AVERAGE", "MAX", "MIN", "MINMAX"]):
            ttk.Radiobutton(mode_box, text=f"rb{name.title()}", value=name, variable=self.sample_mode_var).grid(
                row=0, column=i, sticky="w", padx=8
            )

        btns = ttk.Frame(ctrl)
        btns.grid(row=2, column=0, columnspan=6, sticky="w", pady=(10, 0))
        ttk.Button(btns, text="Start", command=self.start_stream).pack(side="left", padx=(0, 10))
        ttk.Button(btns, text="Stop", command=self.stop_stream).pack(side="left", padx=(0, 10))

        # Display / conversion box
        disp = ttk.LabelFrame(ctrl, text="Display / Conversion", padding=8)
        disp.grid(row=3, column=0, columnspan=6, sticky="we", pady=(10, 0))

        ttk.Label(disp, text="Y-axis:").grid(row=0, column=0, sticky="w")
        ttk.Combobox(
            disp,
            textvariable=self.display_mode_var,
            values=["RAW12", "mA", "bar"],
            state="readonly",
            width=8
        ).grid(row=0, column=1, sticky="w", padx=5)

        ttk.Label(disp, text="V_FS:").grid(row=0, column=2, sticky="e", padx=(10, 2))
        ttk.Entry(disp, textvariable=self.vfs_var, width=8).grid(row=0, column=3, sticky="w")
        ttk.Label(disp, text="V").grid(row=0, column=4, sticky="w")

        ttk.Label(disp, text="R_shunt:").grid(row=0, column=5, sticky="e", padx=(10, 2))
        ttk.Entry(disp, textvariable=self.rshunt_var, width=8).grid(row=0, column=6, sticky="w")
        ttk.Label(disp, text="Ω").grid(row=0, column=7, sticky="w")

        ttk.Label(disp, text="mA→bar Faktor:").grid(row=1, column=0, sticky="w", pady=(8, 0))
        ttk.Entry(disp, textvariable=self.pressure_factor_var, width=8).grid(
            row=1, column=1, sticky="w", padx=5, pady=(8, 0)
        )
        ttk.Label(disp, text="bar/mA").grid(row=1, column=2, sticky="w", pady=(8, 0))

        # Min/Max frame
        mmf = ttk.LabelFrame(outer, text="Empfangene Min/Max-Werte", padding=10)
        mmf.pack(fill="x", pady=(10, 0))

        ttk.Label(mmf, text="Min:").grid(row=0, column=0, sticky="w")
        ttk.Label(mmf, textvariable=self.min_value_var, width=14).grid(row=0, column=1, sticky="w", padx=(5, 15))

        ttk.Label(mmf, text="Max:").grid(row=0, column=2, sticky="w")
        ttk.Label(mmf, textvariable=self.max_value_var, width=14).grid(row=0, column=3, sticky="w", padx=(5, 15))

        ttk.Label(mmf, text="Einheit:").grid(row=0, column=4, sticky="w")
        ttk.Label(mmf, textvariable=self.minmax_unit_var, width=10).grid(row=0, column=5, sticky="w", padx=(5, 15))

        ttk.Button(mmf, text="Min/Max zurücksetzen", command=self.reset_minmax).grid(
            row=0, column=6, sticky="w", padx=(10, 0)
        )

        # Axis frame
        axf = ttk.LabelFrame(outer, text="Axis Scaling", padding=10)
        axf.pack(fill="x", pady=(10, 0))

        ttk.Checkbutton(axf, text="Fixed axis", variable=self.fixed_axis_var).grid(row=0, column=0, sticky="w")

        ttk.Label(axf, text="Xmin").grid(row=0, column=1, sticky="e", padx=(20, 2))
        ttk.Entry(axf, textvariable=self.xmin_var, width=8).grid(row=0, column=2, sticky="w")

        ttk.Label(axf, text="Xmax").grid(row=0, column=3, sticky="e", padx=(10, 2))
        ttk.Entry(axf, textvariable=self.xmax_var, width=8).grid(row=0, column=4, sticky="w")

        ttk.Label(axf, text="Ymin").grid(row=0, column=5, sticky="e", padx=(20, 2))
        ttk.Entry(axf, textvariable=self.ymin_var, width=8).grid(row=0, column=6, sticky="w")

        ttk.Label(axf, text="Ymax").grid(row=0, column=7, sticky="e", padx=(10, 2))
        ttk.Entry(axf, textvariable=self.ymax_var, width=8).grid(row=0, column=8, sticky="w")

        self.plot_frame = ttk.Frame(outer)
        self.plot_frame.pack(fill="both", expand=True, pady=(10, 0))

    def _init_plot(self):
        self.fig = Figure(figsize=(8, 5), dpi=100)
        self.ax = self.fig.add_subplot(111)
        self.ax.set_title("Scope")
        self.ax.set_xlabel("Sample index")
        self.ax.set_ylabel("Current (mA) [calibrated]")
        self.line, = self.ax.plot([], [])

        self.canvas = FigureCanvasTkAgg(self.fig, master=self.plot_frame)
        self.canvas.get_tk_widget().pack(fill="both", expand=True)
        self.canvas.draw()

    # --------------------------------------------------------------- Device IO
    def connect(self):
        self.disconnect()

        ip = self.ip_var.get().strip()
        try:
            port = int(self.port_var.get().strip())
        except ValueError:
            messagebox.showerror("Invalid Port", "Port must be an integer.")
            return

        self.vas = MeasurementDevice(address=ip, port=port, timeout=5.0)
        if self.vas.socket is None:
            self.vas = None
            messagebox.showerror("Connection failed", f"Could not connect to {ip}:{port}")
            return

        try:
            connected = self.vas.ServiceGetConnectedSensors()
            self.detected_dt1_var.set(f"0x{connected['DT_1']:04X}")
            self.detected_dt2_var.set(f"0x{connected['DT_2']:04X}")

            pressure30 = self.vas.CheckPressure30Sensor()
            if pressure30["DT_1"]:
                self.detected_socket_var.set("DT_1")
            elif pressure30["DT_2"]:
                self.detected_socket_var.set("DT_2")
            else:
                self.detected_socket_var.set("not found")

        except MeasurementDeviceError as e:
            messagebox.showwarning("Connected, but query failed", str(e))

    def disconnect(self):
        self.stop_stream()
        if self.vas is not None:
            try:
                self.vas.close()
            finally:
                self.vas = None
        self.detected_socket_var.set("(not connected)")
        self.detected_dt1_var.set("----")
        self.detected_dt2_var.set("----")

    def _get_dt_socket_for_0x08(self) -> str | None:
        if self.vas is None:
            return None
        pressure30 = self.vas.CheckPressure30Sensor()
        if pressure30["DT_1"]:
            return "DT_1"
        if pressure30["DT_2"]:
            return "DT_2"
        return None

    def start_stream(self):
        if self.vas is None:
            messagebox.showerror("Not connected", "Connect to the device first.")
            return
        if self.reader_thread and self.reader_thread.is_alive():
            return

        dt_socket = self._get_dt_socket_for_0x08()
        if dt_socket is None:
            messagebox.showerror("Sensor not found", "No sensor with ID 0x08 detected on DT_1/DT_2.")
            return

        try:
            count = int(self.count_var.get().strip())
            if count <= 0 or count > 2000000:
                raise ValueError
        except ValueError:
            messagebox.showerror("Invalid Count", "Count must be a positive integer.")
            return

        self.ax.set_title(f"Scope A @ {dt_socket} / DRUCK_30 (0x08)")

        self.stop_event.clear()
        self.reader_thread = threading.Thread(target=self._reader_worker, args=(dt_socket,), daemon=True)
        self.reader_thread.start()

    def stop_stream(self):
        self.stop_event.set()
        if self.reader_thread and self.reader_thread.is_alive():
            self.reader_thread.join(timeout=1.0)
        self.reader_thread = None

        if self.vas is not None:
            try:
                self.vas.Scope_Stop()
            except Exception:
                pass
            try:
                self.vas.Scope_Finish()
            except Exception:
                pass

    def reset_minmax(self):
        self.global_min_value = None
        self.global_max_value = None
        self.min_value_var.set("---")
        self.max_value_var.set("---")
        self.minmax_unit_var.set(self._get_display_unit())

    def _get_float_setting(self, var: tk.StringVar, fallback: float) -> float:
        try:
            return float(var.get().strip())
        except Exception:
            return fallback

    def _get_display_unit(self) -> str:
        mode = self.display_mode_var.get().strip().upper()
        if mode == "MA":
            return "mA"
        if mode == "BAR":
            return "bar"
        return "raw12"

    def _format_value_for_display(self, value: float, unit: str) -> str:
        if unit == "raw12":
            return f"{value:.0f}"
        return f"{value:.3f}"

    def _set_ylabel_for_mode(self, disp_mode: str):
        mode = disp_mode.strip().upper()
        if mode == "MA":
            self.ax.set_ylabel("Current (mA) [calibrated]")
        elif mode == "BAR":
            self.ax.set_ylabel("Pressure (bar) [calibrated]")
        else:
            self.ax.set_ylabel("Raw12 (value bits)")

    def _update_minmax_display(self):
        unit = self._get_display_unit()
        self.minmax_unit_var.set(unit)

        if self.global_min_value is None or self.global_max_value is None:
            self.min_value_var.set("---")
            self.max_value_var.set("---")
            return

        self.min_value_var.set(self._format_value_for_display(self.global_min_value, unit))
        self.max_value_var.set(self._format_value_for_display(self.global_max_value, unit))

    def _update_global_minmax(self, y_values: list[float]):
        if not y_values:
            return

        current_min = min(y_values)
        current_max = max(y_values)

        if self.global_min_value is None or current_min < self.global_min_value:
            self.global_min_value = current_min

        if self.global_max_value is None or current_max > self.global_max_value:
            self.global_max_value = current_max

        self._update_minmax_display()

    def _reader_worker(self, dt_socket: str):
        assert self.vas is not None

        channel = self.channel_var.get().strip() or "A"
        socket_name = dt_socket
        sensor = "DRUCK_30"

        range_name = self.range_var.get().strip() or "NO"
        coupling = self.coupling_var.get().strip() or "DC"
        filter_name = self.filter_var.get().strip() or "1MHz"
        mode = self.mode_var.get().strip() or "MANUAL"

        sample_rate = self.sample_rate_var.get().strip()
        sample_method = self.sample_mode_var.get().strip()
        count = int(self.count_var.get().strip())

        def setup_scope():
            self.vas.Scope_SetChannel(channel, socket_name, sensor, range_name, coupling, filter_name, mode)
            self.vas.Scope_Prepare(sample_rate, sample_method, count)

        try:
            setup_scope()
            self.vas.Scope_Start()

            while not self.stop_event.is_set():
                frame = self.vas.Scope_ReceiveData()
                if frame is None:
                    continue

                decoded = decode_scope_samples(frame)
                raw12 = decoded["raw12"]
                marks = decoded["marks"]
                mode_str = decoded["mode"]

                valid_raw12 = filter_valid_by_marks(raw12, marks)

                v_fs = self._get_float_setting(self.vfs_var, V_FS_DEFAULT)
                r_shunt = self._get_float_setting(self.rshunt_var, R_SHUNT_DEFAULT)
                pressure_factor = self._get_float_setting(self.pressure_factor_var, PRESSURE_FACTOR_DEFAULT)

                cal_off = frame.get("CalOffset", 0)
                cal_gain = frame.get("CalGain", 0)
                ch_off = frame.get("CalcOffsetScopeChannel", 0)
                ch_gain = frame.get("CalcGainScopeChannel", 0)

                full_gain, full_offset_ppm = compute_full_gain_and_offset_ppm(frame)

                disp_mode = self.display_mode_var.get().strip().upper()

                if disp_mode == "MA":
                    y: list[float] = []
                    for v12 in valid_raw12:
                        v = raw12_to_voltage(v12, v_fs)
                        v_corr = apply_calibration_to_voltage(v, v_fs, full_gain, full_offset_ppm)
                        i_ma = voltage_to_mA(v_corr, r_shunt)
                        y.append(i_ma)
                elif disp_mode == "BAR":
                    y = []
                    for v12 in valid_raw12:
                        v = raw12_to_voltage(v12, v_fs)
                        v_corr = apply_calibration_to_voltage(v, v_fs, full_gain, full_offset_ppm)
                        i_ma = voltage_to_mA(v_corr, r_shunt)
                        p_bar = current_to_pressure_bar(i_ma, pressure_factor)
                        y.append(p_bar)
                else:
                    y = [float(v) for v in valid_raw12]

                self.latest_y = y
                self._set_ylabel_for_mode(disp_mode)
                self._update_global_minmax(y)

                now = time.time()
                if now - self._last_debug_t >= self._debug_interval_s:
                    self._last_debug_t = now

                    if valid_raw12:
                        vmin = min(valid_raw12)
                        vmax = max(valid_raw12)
                        vavg = sum(valid_raw12) / float(len(valid_raw12))

                        vavg_volt_raw = raw12_to_voltage(int(vavg), v_fs)
                        iavg_ma_raw = voltage_to_mA(vavg_volt_raw, r_shunt)

                        vavg_volt_corr = apply_calibration_to_voltage(vavg_volt_raw, v_fs, full_gain, full_offset_ppm)
                        iavg_ma_corr = voltage_to_mA(vavg_volt_corr, r_shunt)
                        pavg_bar_corr = current_to_pressure_bar(iavg_ma_corr, pressure_factor)
                    else:
                        vmin = vmax = 0
                        vavg = 0.0
                        vavg_volt_raw = 0.0
                        iavg_ma_raw = 0.0
                        vavg_volt_corr = 0.0
                        iavg_ma_corr = 0.0
                        pavg_bar_corr = 0.0

                    preview_n = min(8, len(raw12))
                    preview_vals = raw12[:preview_n]
                    preview_marks = marks[:preview_n] if marks is not None else None

                    print(
                        f"[{mode_str}] Count={frame.get('Count')} DataLen={len(frame.get('Data', b''))} "
                        f"valid_raw[min,max,avg]=({vmin},{vmax},{vavg:.2f}) "
                        f"V_FS={v_fs}V Rshunt={r_shunt}Ω PressureFactor={pressure_factor}bar/mA"
                    )
                    print(
                        f"  CalOffset={cal_off} CalGain={cal_gain} "
                        f"ChOffset={ch_off} ChGain={ch_gain} (GAIN_DIV={GAIN_DIV:g})"
                    )
                    print(
                        f"  fullGain={full_gain:.10f} fullOffset_ppm={full_offset_ppm:.3f}  "
                        f"offset_as_volts={(full_offset_ppm / GAIN_DIV) * v_fs:.6f}V"
                    )
                    print(
                        f"  avgV_raw={vavg_volt_raw:.6f}V  avgI_raw={iavg_ma_raw:.3f}mA  |  "
                        f"avgV_cal={vavg_volt_corr:.6f}V  avgI_cal={iavg_ma_corr:.3f}mA  "
                        f"avgP_cal={pavg_bar_corr:.3f}bar"
                    )
                    print(f"  preview raw12 : {preview_vals}")
                    if preview_marks is not None:
                        print(f"  preview marks : {preview_marks}")
                    else:
                        pairs = decoded.get("pairs")
                        if pairs:
                            print(f"  preview (low16,high16): {pairs[:min(4, len(pairs))]}")

                time.sleep(0.001)

        except MeasurementDeviceError as e:
            msg = str(e)

            if "2027" in msg or "0x2027" in msg:
                try:
                    self.vas.Scope_Stop()
                except Exception:
                    pass
                try:
                    self.vas.Scope_Finish()
                except Exception:
                    pass
                if not self.stop_event.is_set():
                    try:
                        setup_scope()
                        self.vas.Scope_Start()
                        return
                    except Exception:
                        pass

            self.after(0, lambda: messagebox.showerror("Device error", msg))

        finally:
            try:
                self.vas.Scope_Stop()
            except Exception:
                pass
            try:
                self.vas.Scope_Finish()
            except Exception:
                pass

    # --------------------------------------------------------------- Plot loop
    def _update_plot_loop(self):
        try:
            self._set_ylabel_for_mode(self.display_mode_var.get())
            self._update_minmax_display()

            if self.latest_y is not None:
                y = self.latest_y
                x = list(range(len(y)))
                self.line.set_data(x, y)

                if self.fixed_axis_var.get():
                    try:
                        xmin = float(self.xmin_var.get())
                        xmax = float(self.xmax_var.get())
                        ymin = float(self.ymin_var.get())
                        ymax = float(self.ymax_var.get())
                        if xmax <= xmin:
                            xmax = xmin + 1
                        if ymax <= ymin:
                            ymax = ymin + 1
                        self.ax.set_xlim(xmin, xmax)
                        self.ax.set_ylim(ymin, ymax)
                    except ValueError:
                        pass
                else:
                    self.ax.relim()
                    self.ax.autoscale_view()

                self.canvas.draw_idle()

        finally:
            self.after(30, self._update_plot_loop)

    def on_close(self):
        self.disconnect()
        self.destroy()


if __name__ == "__main__":
    app = ScopeGUI()
    app.mainloop()