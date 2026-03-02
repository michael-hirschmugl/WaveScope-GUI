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


def decode_scope_samples(frame: dict) -> list[int]:
    """
    Convert packed scope sample bytes into Python ints.

    - AVERAGE -> int16 (2 bytes/sample)
    - others  -> int32 (4 bytes/sample)
    """
    data: bytes = frame["Data"]
    sample_method = frame["SampleMethod"]

    if sample_method == MeasurementDevice.scope_sample_methods["AVERAGE"]:
        sample_size = 2
        fmt = ">h"
    else:
        sample_size = 4
        fmt = ">i"

    n = len(data) // sample_size
    out = []
    for i in range(n):
        (v,) = struct.unpack(fmt, data[i * sample_size : (i + 1) * sample_size])
        out.append(v)
    return out


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

        self._build_ui()
        self.protocol("WM_DELETE_WINDOW", self.on_close)

        # Matplotlib plot
        self._init_plot()

        # Plot update timer
        self.latest_y: list[int] | None = None
        self.after(30, self._update_plot_loop)

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

        # Sample mode radios (like your screenshot)
        mode_box = ttk.LabelFrame(ctrl, text="SampleMode", padding=8)
        mode_box.grid(row=1, column=0, columnspan=6, sticky="we", pady=(10, 0))

        for i, name in enumerate(["AVERAGE", "MAX", "MIN", "MINMAX"]):
            ttk.Radiobutton(mode_box, text=f"rb{name.title()}", value=name, variable=self.sample_mode_var).grid(
                row=0, column=i, sticky="w", padx=8
            )

        # Start/Stop buttons
        btns = ttk.Frame(ctrl)
        btns.grid(row=2, column=0, columnspan=6, sticky="w", pady=(10, 0))
        ttk.Button(btns, text="Start", command=self.start_stream).pack(side="left", padx=(0, 10))
        ttk.Button(btns, text="Stop", command=self.stop_stream).pack(side="left")

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
        self.ax.set_title("Scope (raw samples)")
        self.ax.set_xlabel("Sample index")
        self.ax.set_ylabel("Raw value")
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

                y = decode_scope_samples(frame)
                self.latest_y = y
                print(
                "raw[min,max]=", min(y), max(y),
                "CalOff=", frame["CalOffset"],
                "CalGain=", frame["CalGain"],
                "ChOff=", frame["CalcOffsetScopeChannel"],
                "ChGain=", frame["CalcGainScopeChannel"],
                
                print("Count=", frame["Count"], "DataLen=", len(frame["Data"]), "SampleMethod=", frame["SampleMethod"])
)

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