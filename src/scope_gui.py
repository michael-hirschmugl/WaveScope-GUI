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


# ---------------------------------------------------------------------------
# Scope payload decoding helpers
#
# Firmware behavior (as described):
#   - AVERAGE: payload is Count * uint16
#       bits 0..11  = value (12-bit)
#       bits 12..15 = marker/trigger flags (4-bit)
#
#   - MIN / MAX / MINMAX: payload is Count * uint32
#       Usually two uint16 packed inside one uint32 (low16 / high16).
#       Semantics (min vs max) may depend on mode; you can verify in practice.
#
# All values are big-endian.
# ---------------------------------------------------------------------------

def _gain_factor_ppm(gain_ppm: int) -> float:
    """Firmware-style gain factor: 1 + gain/100000."""
    return 1.0 + (float(gain_ppm) / 100000.0)


def compute_full_gain_offset(frame: dict) -> tuple[float, float]:
    """
    Compute combined gain/offset based on the calibration fields in the frame.

    Fields (from frame dict):
      - CalOffset  (extOffset)               int16
      - CalGain    (extGain)                 int16
      - CalcOffsetScopeChannel (intOffset)   int16
      - CalcGainScopeChannel   (intGain)     int16

    Firmware math:
      fullGain   = (1 + intGain/100000) * (1 + extGain/100000)
      fullOffset = fullGain * intOffset + extOffset
    """
    ext_off = int(frame.get("CalOffset", 0))
    ext_gain = int(frame.get("CalGain", 0))
    int_off = int(frame.get("CalcOffsetScopeChannel", 0))
    int_gain = int(frame.get("CalcGainScopeChannel", 0))

    full_gain = _gain_factor_ppm(int_gain) * _gain_factor_ppm(ext_gain)
    full_offset = (full_gain * float(int_off)) + float(ext_off)
    return full_gain, full_offset


def apply_calibration_to_values(values: list[float], full_gain: float, full_offset: float) -> list[float]:
    """
    Apply linear correction:
      corrected = fullGain * raw + fullOffset

    Note: raw should be the *value bits only* (no marker bits).
    """
    return [(full_gain * v) + full_offset for v in values]


def decode_scope_samples(frame: dict) -> dict:
    """
    Decode scope samples from one received frame.

    Returns a dict with:
      - "mode": str (AVERAGE / MIN / MAX / MINMAX / unknown)
      - "values": list[int]        (decoded "value" samples; for non-AVERAGE this is a chosen view)
      - "marks": list[int] | None  (only for AVERAGE, extracted 4-bit marker flags)
      - "pairs": list[tuple[int,int]] | None  (for 32-bit modes: (low16, high16) per sample)
      - "pairs_value_marks": list[tuple[tuple[int,int], tuple[int,int]]] | None
            ((low_value12, high_value12), (low_marks, high_marks)) if you want to inspect flags in halves
    """
    data: bytes = frame["Data"]
    sample_method_id = frame["SampleMethod"]

    # Resolve method name if possible
    method_name = None
    try:
        for k, v in MeasurementDevice.scope_sample_methods.items():
            if v == sample_method_id:
                method_name = k
                break
    except Exception:
        method_name = None
    mode = method_name or f"UNKNOWN({sample_method_id})"

    # AVERAGE: Count * uint16, with 12-bit value + 4-bit markers
    if sample_method_id == MeasurementDevice.scope_sample_methods.get("AVERAGE"):
        sample_size = 2
        n = len(data) // sample_size

        values12: list[int] = []
        marks: list[int] = []

        for i in range(n):
            (u16,) = struct.unpack(">H", data[i * 2:(i + 1) * 2])
            values12.append(u16 & 0x0FFF)
            marks.append((u16 >> 12) & 0x000F)

        return {
            "mode": mode,
            "values": values12,
            "marks": marks,
            "pairs": None,
            "pairs_value_marks": None,
        }

    # Other modes: Count * uint32 (two uint16 halves)
    sample_size = 4
    n = len(data) // sample_size

    pairs: list[tuple[int, int]] = []
    pairs_value_marks: list[tuple[tuple[int, int], tuple[int, int]]] = []

    for i in range(n):
        (u32,) = struct.unpack(">I", data[i * 4:(i + 1) * 4])
        low16 = u32 & 0xFFFF
        high16 = (u32 >> 16) & 0xFFFF
        pairs.append((low16, high16))

        # If the FPGA also encodes marker bits in the 16-bit halves, you can inspect them:
        low_val12 = low16 & 0x0FFF
        low_marks = (low16 >> 12) & 0x000F
        high_val12 = high16 & 0x0FFF
        high_marks = (high16 >> 12) & 0x000F
        pairs_value_marks.append(((low_val12, high_val12), (low_marks, high_marks)))

    # For plotting we choose one view. For MINMAX it is often useful to plot the midpoint.
    # You can change this easily once you confirm semantics.
    values_view: list[int] = []
    if method_name == "MINMAX":
        for low16, high16 in pairs:
            # midpoint of the two halves (works nicely for a “single trace”)
            values_view.append(((low16 & 0x0FFF) + (high16 & 0x0FFF)) // 2)
    elif method_name == "MIN":
        for low16, high16 in pairs:
            values_view.append(low16 & 0x0FFF)
    elif method_name == "MAX":
        for low16, high16 in pairs:
            values_view.append(high16 & 0x0FFF)
    else:
        for low16, high16 in pairs:
            values_view.append(low16 & 0x0FFF)

    return {
        "mode": mode,
        "values": values_view,
        "marks": None,
        "pairs": pairs,
        "pairs_value_marks": pairs_value_marks,
    }


class ScopeGUI(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("WaveScope GUI (DT Pressure Sensor 0x08)")
        self.geometry("1100x700")

        self.vas: MeasurementDevice | None = None
        self.reader_thread: threading.Thread | None = None
        self.stop_event = threading.Event()

        # ---- UI state -------------------------------------------------------
        self.ip_var = tk.StringVar(value="192.168.111.111")
        self.port_var = tk.StringVar(value="55555")

        # Defaults that worked for you
        self.channel_var = tk.StringVar(value="A")
        self.range_var = tk.StringVar(value="NO")          # keep as in your working config
        self.coupling_var = tk.StringVar(value="DC")
        self.mode_var = tk.StringVar(value="MANUAL")

        # Filter dropdown like in your screenshot
        self.filter_var = tk.StringVar(value="1MHz")

        # Sample controls
        self.sample_rate_var = tk.StringVar(value="20MS")
        self.sample_mode_var = tk.StringVar(value="AVERAGE")
        self.count_var = tk.StringVar(value="1000")

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

        # Plot options
        self.plot_calibrated_var = tk.BooleanVar(value=False)  # plot corrected instead of raw
        self.export_csv_var = tk.BooleanVar(value=True)        # write a small CSV snippet periodically

        self._build_ui()
        self.protocol("WM_DELETE_WINDOW", self.on_close)

        # Matplotlib plot
        self._init_plot()

        # Plot update timer
        self.latest_y: list[float] | None = None
        self.after(30, self._update_plot_loop)

        # Throttle debug prints / CSV writes
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

        # Filter dropdown
        ttk.Label(ctrl, text="Filter:").grid(row=0, column=0, sticky="w")
        filters = list(MeasurementDevice.scope_filters.keys())
        ttk.Combobox(ctrl, textvariable=self.filter_var, values=filters, state="readonly", width=12).grid(
            row=0, column=1, sticky="w", padx=5
        )

        # Sampling rate dropdown
        ttk.Label(ctrl, text="SampleRate:").grid(row=0, column=2, sticky="w")
        rates = list(MeasurementDevice.scope_sample_rates.keys())
        ttk.Combobox(ctrl, textvariable=self.sample_rate_var, values=rates, state="readonly", width=12).grid(
            row=0, column=3, sticky="w", padx=5
        )

        # Count
        ttk.Label(ctrl, text="Count:").grid(row=0, column=4, sticky="w")
        ttk.Entry(ctrl, textvariable=self.count_var, width=8).grid(row=0, column=5, sticky="w", padx=5)

        # Sample mode radios
        mode_box = ttk.LabelFrame(ctrl, text="SampleMode", padding=8)
        mode_box.grid(row=1, column=0, columnspan=6, sticky="we", pady=(10, 0))

        for i, name in enumerate(["AVERAGE", "MAX", "MIN", "MINMAX"]):
            ttk.Radiobutton(mode_box, text=f"rb{name.title()}", value=name, variable=self.sample_mode_var).grid(
                row=0, column=i, sticky="w", padx=8
            )

        # Start/Stop + plot options
        btns = ttk.Frame(ctrl)
        btns.grid(row=2, column=0, columnspan=6, sticky="w", pady=(10, 0))
        ttk.Button(btns, text="Start", command=self.start_stream).pack(side="left", padx=(0, 10))
        ttk.Button(btns, text="Stop", command=self.stop_stream).pack(side="left", padx=(0, 10))

        ttk.Checkbutton(btns, text="Plot calibrated", variable=self.plot_calibrated_var).pack(side="left", padx=(0, 10))
        ttk.Checkbutton(btns, text="Write CSV snippet", variable=self.export_csv_var).pack(side="left")

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

        # Plot area frame
        self.plot_frame = ttk.Frame(outer)
        self.plot_frame.pack(fill="both", expand=True, pady=(10, 0))

    def _init_plot(self):
        self.fig = Figure(figsize=(8, 5), dpi=100)
        self.ax = self.fig.add_subplot(111)
        self.ax.set_title("Scope (samples)")
        self.ax.set_xlabel("Sample index")
        self.ax.set_ylabel("Value")
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
            # Check connected sensors + 0x08 position
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

        # Validate count
        try:
            count = int(self.count_var.get().strip())
            if count <= 0 or count > 2000000:
                raise ValueError
        except ValueError:
            messagebox.showerror("Invalid Count", "Count must be a positive integer.")
            return

        # Configure plot title
        self.ax.set_title(f"Scope A @ {dt_socket} / DRUCK_30 (0x08)")

        # Start worker thread
        self.stop_event.clear()
        self.reader_thread = threading.Thread(target=self._reader_worker, args=(dt_socket,), daemon=True)
        self.reader_thread.start()

    def stop_stream(self):
        self.stop_event.set()
        if self.reader_thread and self.reader_thread.is_alive():
            self.reader_thread.join(timeout=1.0)
        self.reader_thread = None

        # Cleanup scope once
        if self.vas is not None:
            try:
                self.vas.Scope_Stop()
            except Exception:
                pass
            try:
                self.vas.Scope_Finish()
            except Exception:
                pass

    def _write_csv_snippet(self, mode: str, values: list[float], marks: list[int] | None, full_gain: float, full_offset: float):
        """
        Write a small CSV snippet for easy Excel import.
        Overwrites a file 'scope_snippet.csv' in the current working directory.
        """
        if not self.export_csv_var.get():
            return

        # Keep it small and simple: first N samples.
        N = min(200, len(values))
        path = "scope_snippet.csv"

        with open(path, "w", encoding="utf-8") as f:
            f.write("index,raw_value,corrected_value,marks,mode,fullGain,fullOffset\n")
            for i in range(N):
                raw_v = values[i]
                corr_v = (full_gain * raw_v) + full_offset
                mk = marks[i] if (marks is not None and i < len(marks)) else ""
                f.write(f"{i},{raw_v},{corr_v},{mk},{mode},{full_gain},{full_offset}\n")

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
            # Important: apply the exact settings that the firmware supports
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
                values = decoded["values"]          # raw 12-bit (or a chosen view for 32-bit modes)
                marks = decoded["marks"]            # only for AVERAGE
                method_str = decoded["mode"]

                # Compute calibration coefficients from the frame
                full_gain, full_offset = compute_full_gain_offset(frame)

                # Decide what to plot
                if self.plot_calibrated_var.get():
                    y_plot = apply_calibration_to_values([float(v) for v in values], full_gain, full_offset)
                    self.ax.set_ylabel("Corrected value (gain/offset applied)")
                else:
                    y_plot = [float(v) for v in values]
                    self.ax.set_ylabel("Raw value (value bits only)")

                self.latest_y = y_plot

                # Debug print (throttled) + CSV snippet (throttled)
                now = time.time()
                if now - self._last_debug_t >= self._debug_interval_s:
                    self._last_debug_t = now

                    # Some quick stats for Excel testing
                    if values:
                        vmin = min(values)
                        vmax = max(values)
                        vavg = sum(values) / float(len(values))
                    else:
                        vmin = vmax = vavg = float("nan")

                    # Show first few samples for easy manual inspection
                    preview_n = min(8, len(values))
                    preview_vals = values[:preview_n]
                    preview_marks = marks[:preview_n] if marks is not None else None

                    print(
                        f"[{method_str}] Count={frame.get('Count')} DataLen={len(frame.get('Data', b''))} "
                        f"raw[min,max,avg]=({vmin},{vmax},{vavg:.2f}) "
                        f"fullGain={full_gain:.8f} fullOffset={full_offset:.3f} "
                        f"extOff={frame.get('CalOffset')} extGain={frame.get('CalGain')} "
                        f"intOff={frame.get('CalcOffsetScopeChannel')} intGain={frame.get('CalcGainScopeChannel')}"
                    )
                    print(f"  preview raw values: {preview_vals}")
                    if preview_marks is not None:
                        print(f"  preview marks     : {preview_marks}")
                    else:
                        # For 32-bit modes, also show a couple of low/high halves
                        pairs = decoded.get("pairs")
                        if pairs:
                            print(f"  preview (low16,high16): {pairs[:min(4, len(pairs))]}")

                    # Write CSV for Excel (first 200 samples)
                    self._write_csv_snippet(method_str, [float(v) for v in values], marks, full_gain, full_offset)

                # keep UI responsive; don't spin at 100%
                time.sleep(0.001)

        except MeasurementDeviceError as e:
            msg = str(e)

            # Recovery: if channel setup was lost, re-init.
            if "2027" in msg or "0x2027" in msg:
                # Try a soft restart
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
                        return  # let outer loop end; user can press Start again if needed
                    except Exception:
                        pass

            # Show error in UI thread
            self.after(0, lambda: messagebox.showerror("Device error", msg))

        finally:
            # Best-effort cleanup
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
                    # Fixed axis
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
                        # If user types garbage, keep the last valid limits
                        pass
                else:
                    # Autoscale
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