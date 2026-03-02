import sys
import time
import struct

from lib.MeasurementDevice import MeasurementDevice, MeasurementDeviceError


def _decode_scope_samples(frame: dict) -> list[int]:
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


def main():
    ip = sys.argv[1] if len(sys.argv) > 1 else "192.168.111.111"
    port = int(sys.argv[2]) if len(sys.argv) > 2 else 55555

    vas = MeasurementDevice(address=ip, port=port, timeout=5.0)

    if vas.socket is None:
        print(f"Could not connect to {ip}:{port}")
        sys.exit(1)

    # -------- Scope config ---------------------------------------------------
    channel = "A"
    range_name = "NO"
    coupling = "DC"
    filter_name = "1MHz"
    mode = "MANUAL"

    sample_rate = "20MS"
    sample_method = "AVERAGE"
    count = 1000

    # For nicer plots, remember last N frames (optional)
    # max_points = 2000

    try:
        info = vas.ServiceGetInfo()
        print("ServiceGetInfo():")
        for key, value in info.items():
            print(f"  {key}: {value}")

        try:
            hw = vas.ServiceGetHWVersion()
            print("\nServiceGetHWVersion():")
            for key, value in hw.items():
                print(f"  {key}: {value}")
        except MeasurementDeviceError as e:
            print(f"\nServiceGetHWVersion failed: {e}")

        connected = vas.ServiceGetConnectedSensors()
        print("\nServiceGetConnectedSensors():")
        print(f"  DT_1: 0x{connected['DT_1']:04X}")
        print(f"  DT_2: 0x{connected['DT_2']:04X}")

        pressure30 = vas.CheckPressure30Sensor()
        print("\n30 bar Sensor (DRUCK_30 / ID 0x08) connected?")
        print(f"  DT_1: {pressure30['DT_1']}")
        print(f"  DT_2: {pressure30['DT_2']}")
        print(f"  any : {pressure30['any']}")

        if not pressure30["any"]:
            print("\nNo 0x08 sensor found on DT_1/DT_2. Not starting scope.")
            return

        socket_name = "DT_1" if pressure30["DT_1"] else "DT_2"
        sensor = "DRUCK_30"

        print(f"\nStarting streaming scope plot from {socket_name} (sensor DRUCK_30 / 0x08).")

        import matplotlib.pyplot as plt

        plt.ion()
        fig, ax = plt.subplots()
        ax.set_title(f"Scope {channel} @ {socket_name} / {sensor} (raw)")
        ax.set_xlabel("Sample index")
        ax.set_ylabel("Raw value")
        (line,) = ax.plot([], [])
        fig.show()

        def start_stream():
            # Set up once, then start acquisition and keep it running
            vas.Scope_SetChannel(channel, socket_name, sensor, range_name, coupling, filter_name, mode)
            vas.Scope_Prepare(sample_rate, sample_method, count)
            vas.Scope_Start()

        def stop_stream():
            try:
                vas.Scope_Stop()
            except Exception:
                pass
            try:
                vas.Scope_Finish()
            except Exception:
                pass

        # Start once
        start_stream()

        try:
            while True:
                try:
                    frame = vas.Scope_ReceiveData()
                    if frame is None:
                        # No frame arrived within socket timeout: just continue waiting
                        # (optional: print a dot every few seconds)
                        continue

                    y = _decode_scope_samples(frame)
                    x = list(range(len(y)))

                    line.set_data(x, y)
                    ax.relim()
                    ax.autoscale_view()
                    fig.canvas.draw()
                    fig.canvas.flush_events()

                    # Small sleep keeps UI responsive; can be 0 for max speed
                    time.sleep(0.001)

                except MeasurementDeviceError as e:
                    msg = str(e)
                    # If the device "forgets" channel setup mid-stream, re-init everything
                    if "2027" in msg or "0x2027" in msg:
                        print("Scope error 0x2027 (NO_CHANNEL_SETUP) -> restarting stream...")
                        stop_stream()
                        start_stream()
                        continue
                    raise

        except KeyboardInterrupt:
            print("\nStopped by user (Ctrl+C).")

        finally:
            stop_stream()

    except MeasurementDeviceError as e:
        print(f"Error: {e}")
        sys.exit(2)

    finally:
        vas.close()


if __name__ == "__main__":
    main()