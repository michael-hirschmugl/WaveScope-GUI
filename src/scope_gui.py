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
# Conversion model used here (based on firmware conventions for DT pressure):
#
# AVERAGE payload:
#   uint16 per sample:
#     bits 0..11  -> raw12 value (0..4095)
#     bits 12..15 -> marker flags (0..15)
#
# For plotting/metrics we ignore samples with marks != 0 (trigger/marker events).
#
# RAW(12-bit) -> Voltage:
#   V = (raw12 - 2048) / 2048.0 * V_FS
#
# For DT pressure/current front-end the effective full-scale used in practice is
# typically 1.0 V (fits your 4 mA example well). If your firmware/hardware
# variant uses 1.5 V, change V_FS below or make it a UI option.
#
# Voltage -> Current:
#   I_mA = (V / R_SHUNT) * 1000
#
# Default R_SHUNT is 49.9 Ohm (typical 4..20 mA shunt).
# ============================================================================

V_FS_DEFAULT = 1.0       # Volts full-scale used for raw12->V mapping
R_SHUNT_DEFAULT = 49.9   # Ohms shunt for V->I conversion


def raw12_to_voltage(raw12: int, v_fs: float = V_FS_DEFAULT) -> float:
    """Convert 12-bit offset-binary code to volts."""
    return ((float(raw12) - 2048.0) / 2048.0) * float(v_fs)


def voltage_to_mA(v: float, r_shunt: float = R_SHUNT_DEFAULT) -> float:
    """Convert volts across shunt to mA."""
    return (float(v) / float(r_shunt)) * 1000.0


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

    # resolve method name if possible
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

    # MIN/MAX/MINMAX (or other): uint32 per sample
    n = len(data) // 4
    pairs: list[tuple[int, int]] = []
    for i in range(n):
        (u32,) = struct.unpack(">I", data[i * 4:(i + 1) * 4])
        low16 = u32 & 0xFFFF
        high16 = (u32 >> 16) & 0xFFFF
        pairs.append((low16, high16))

    # Choose a simple view for plotting (still in raw12-value domain)
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
        self.geometry("1100x720")

        self.vas: MeasurementDevice | None = None
        self.reader_thread: threading.Thread | None = None
        self.stop_event = threading.Event()

        # ---- UI state -------------------------------------------------------
        self.ip_var = tk.StringVar(value="192.168.111.111")
        self.port_var = tk.StringVar(value="55555")

        self.channel_var = tk.StringVar(value="A")
        self.range_var = tk.StringVar(value="NO")          # keep as in your working config
        self.coupling_var = tk.StringVar(value="DC")
        self.mode_var = tk.StringVar(value="MANUAL")

        self.filter_var = tk.StringVar(value="1MHz")

        self.sample_rate_var = tk.StringVar(value="20MS")
        self.sample_mode_var = tk.StringVar(value="AVERAGE")
        self.count_var = tk.StringVar(value="1000")

        # Display/Conversion controls
        self.display_mode_var = tk.StringVar(value="RAW12")    # RAW12 or mA
        self.vfs_var = tk.StringVar(value=str(V_FS_DEFAULT))   # 1.0 or 1.5 typically
        self.rshunt_var = tk.StringVar(value=str(R_SHUNT_DEFAULT))

        # Axis scaling
        self.fixed_axis_var = tk.BooleanVar(value=True)
        self.xmin_var = tk.StringVar(value="0")
        self.xmax_var = tk.StringVar(value="1000")
        self.ymin_var = tk.StringVar(value="-2000")
        self.ymax_var = tk.StringVar(value="2000")

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
        ttk.Combobox(disp, textvariable=self.display_mode_var, values=["RAW12", "mA"], state="readonly", width=8).grid(
            row=0, column=1, sticky="w", padx=5
        )

        ttk.Label(disp, text="V_FS:").grid(row=0, column=2, sticky="e", padx=(10, 2))
        ttk.Entry(disp, textvariable=self.vfs_var, width=8).grid(row=0, column=3, sticky="w")
        ttk.Label(disp, text="V").grid(row=0, column=4, sticky="w")

        ttk.Label(disp, text="R_shunt:").grid(row=0, column=5, sticky="e", padx=(10, 2))
        ttk.Entry(disp, textvariable=self.rshunt_var, width=8).grid(row=0, column=6, sticky="w")
        ttk.Label(disp, text="Ω").grid(row=0, column=7, sticky="w")

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
        self.ax.set_ylabel("Raw12")
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

    def _get_float_setting(self, var: tk.StringVar, fallback: float) -> float:
        try:
            return float(var.get().strip())
        except Exception:
            return fallback

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

                # ignore marker samples for stats / conversion (AVERAGE only)
                valid_raw12 = filter_valid_by_marks(raw12, marks)

                v_fs = self._get_float_setting(self.vfs_var, V_FS_DEFAULT)
                r_shunt = self._get_float_setting(self.rshunt_var, R_SHUNT_DEFAULT)

                # Choose what to display: RAW12 or mA
                disp_mode = self.display_mode_var.get().strip().upper()
                if disp_mode == "MA":
                    y = [voltage_to_mA(raw12_to_voltage(v, v_fs), r_shunt) for v in valid_raw12]
                    self.ax.set_ylabel("Current (mA)")
                else:
                    y = [float(v) for v in valid_raw12]
                    self.ax.set_ylabel("Raw12 (value bits)")

                self.latest_y = y

                # Throttled debug output
                now = time.time()
                if now - self._last_debug_t >= self._debug_interval_s:
                    self._last_debug_t = now

                    if valid_raw12:
                        vmin = min(valid_raw12)
                        vmax = max(valid_raw12)
                        vavg = sum(valid_raw12) / float(len(valid_raw12))
                        vavg_volt = raw12_to_voltage(int(vavg), v_fs)
                        iavg_ma = voltage_to_mA(vavg_volt, r_shunt)
                    else:
                        vmin = vmax = 0
                        vavg = 0.0
                        vavg_volt = 0.0
                        iavg_ma = 0.0

                    preview_n = min(8, len(raw12))
                    preview_vals = raw12[:preview_n]
                    preview_marks = marks[:preview_n] if marks is not None else None

                    print(
                        f"[{mode_str}] Count={frame.get('Count')} DataLen={len(frame.get('Data', b''))} "
                        f"valid_raw[min,max,avg]=({vmin},{vmax},{vavg:.2f}) "
                        f"V_FS={v_fs}V Rshunt={r_shunt}Ω  avgV={vavg_volt:.6f}V avgI={iavg_ma:.3f}mA"
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