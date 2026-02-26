import sys
from lib.MeasurementDevice import MeasurementDevice, MeasurementDeviceError


def main():
    # Read IP address and port from command line arguments
    # Defaults are used if no arguments are provided
    ip = sys.argv[1] if len(sys.argv) > 1 else "192.168.111.111"
    port = int(sys.argv[2]) if len(sys.argv) > 2 else 55555

    # Create device instance
    vas = MeasurementDevice(address=ip, port=port, timeout=5.0)

    # Check if connection was successful
    if vas.socket is None:
        print(f"Could not connect to {ip}:{port}")
        sys.exit(1)

    try:
        # Retrieve general device information
        info = vas.ServiceGetInfo()
        print("ServiceGetInfo():")
        for key, value in info.items():
            print(f"  {key}: {value}")

        # Optionally retrieve hardware version information
        try:
            hw = vas.ServiceGetHWVersion()
            print("\nServiceGetHWVersion():")
            for key, value in hw.items():
                print(f"  {key}: {value}")
        except MeasurementDeviceError as e:
            print(f"\nServiceGetHWVersion failed: {e}")

    except MeasurementDeviceError as e:
        print(f"Error: {e}")
        sys.exit(2)

    finally:
        # Always close the connection properly
        vas.close()


if __name__ == "__main__":
    main()