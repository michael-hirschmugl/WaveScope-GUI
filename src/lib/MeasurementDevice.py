"""
measurement_device.py

A small Python 3 client library for a TCP-connected measurement instrument.

This module implements the binary protocol used by the device to:
- query device/service information
- configure measurement channels and ranges
- start/stop scope acquisition
- receive waveform data fragments

Notes
-----
- The device protocol uses big-endian ("network order") integer encoding.
- Messages are length-prefixed. The first 2 bytes contain a word-count; total message size is word_count * 2 bytes.
- Many commands return a status/error code at offset 12 (uint16).
- This library is intentionally thin: it focuses on transport + message encoding/decoding.
"""

from __future__ import annotations

import socket
import struct
from typing import Optional, Dict, Any


class MeasurementDeviceError(Exception):
    """Raised for socket issues, protocol timeouts, or device-reported errors."""
    pass


class MeasurementDevice:
    """
    TCP client for a multi-function measurement instrument.

    The public API mirrors the device's command set (Service*, URDI_*, DSO_*, Scope_*).
    """

    # --- Enumerations / lookup tables (string -> protocol id) -----------------
    sockets = {
        "DSO1": 0x01,
        "DSO2": 0x02,
        "IZNG": 0x03,
        "KV": 0x04,
        "DT_1": 0x05,
        "DT_2": 0x06,
        "TRGZNG": 0x07,
        "URDI": 0x08,
    }

    sensors = {
        "DSO": 0x01,
        "KV": 0x02,
        "TRGZNG": 0x03,
        "IZNG_50": 0x01,
        "IZNG_100": 0x02,
        "IZNG_500": 0x03,
        "IZNG_1800": 0x04,
        "DRUCK_1": 0x01,
        "DRUCK_4": 0x02,
        "DRUCK_30": 0x08,  # 30 bar pressure sensor (ID 0x08)
        "DRUCK_60": 0x03,
        "DRUCK_400": 0x04,
        "TEMP_LIQ": 0x05,
        "TEMP_AIR": 0x06,
        "DT_CALIB": 0xF1,
        "TEMP_CALIB": 0xF2,
        "DRUCK_CALIB": 0xF3,
        "DSO_REFGEN": 0xF4,
        "URDI": 0x01,
        "URDI_U": 0x01,
        "URDI_R": 0x02,
        "URDI_D": 0x03,
        "URDI_I": 0x04,
    }

    ranges = {
        "AUTO": 0x00,
        "NO": 0x01,
        "DSO_400MV": 0x01,
        "DSO_1_6V": 0x02,
        "DSO_4V": 0x03,
        "DSO_16V": 0x04,
        "DSO_40V": 0x05,
        "DSO_160V": 0x06,
        "DSO_400V": 0x07,
        "IZNG50_5": 0x01,
        "IZNG50_25": 0x02,
        "IZNG50_50": 0x03,
        "IZNG100_5": 0x01,
        "IZNG100_10": 0x02,
        "IZNG100_100": 0x03,
        "IZNG100_50": 0x04,
        "IZNG500_100": 0x01,
        "IZNG500_250": 0x02,
        "IZNG500_500": 0x03,
        "IZNG1800_1000": 0x01,
        "IZNG1800_2000": 0x02,
        "IZNG1800_200": 0x03,
        "KV_8KV": 0x01,
        "KV_20KV": 0x02,
        "KV_40KV": 0x03,
        "URDI_U_DC_2": 0x01,
        "URDI_U_DC_20": 0x02,
        "URDI_U_DC_50": 0x03,
        "URDI_U_AC_2": 0x01,
        "URDI_U_AC_20": 0x02,
        "URDI_U_AC_40": 0x03,
        "URDI_I_DC_0_2": 0x01,
        "URDI_I_DC_2": 0x02,
        "URDI_I_AC_0_2": 0x01,
        "URDI_I_AC_2": 0x02,
        "URDI_R_10": 0x01,
        "URDI_R_100": 0x02,
        "URDI_R_1K": 0x03,
        "URDI_R_10K": 0x04,
        "URDI_R_100K": 0x05,
        "URDI_R_1M": 0x06,
        "URDI_R_10M": 0x07,
    }

    couplings = {"AC": 1, "DC": 0, "GND": 2}
    modes = {"AUTO": 1, "MANUAL": 0, "SEMI_AUTO": 2}

    trigger_modes = {"AUTOSETUP": 0, "DYNALEVEL": 1, "FIXEDLEVEL": 2}
    trigger_edges = {"LH": 1, "HL": 2}
    trigger_filters = {"DC": 0, "AC": 1, "LF": 2, "HF": 3}

    scope_filters = {"NO": 0, "1MHz": 1, "100kHz": 2, "20kHz": 3, "10kHz": 4, "1kHz": 5}
    scope_sample_methods = {"AVERAGE": 0, "MIN": 1, "MAX": 2, "MINMAX": 3}
    scope_sample_rates = {
        "40MS": 0x00,
        "20MS": 0x01,
        "10MS": 0x02,
        "5MS": 0x03,
        "2.5MS": 0x04,
        "1MS": 0x05,
        "500KS": 0x06,
        "250KS": 0x07,
        "100KS": 0x08,
        "50KS": 0x09,
        "25KS": 0x0A,
        "10KS": 0x0B,
        "5KS": 0x0C,
        "2.5KS": 0x0D,
        "1KS": 0x0E,
        "500S": 0x0F,
        "250S": 0x10,
        "100S": 0x11,
        "50S": 0x12,
        "25S": 0x13,
        "10S": 0x14,
        "5S": 0x15,
        "2.5S": 0x16,
        "1S": 0x17,
        "0.5S": 0x18,
    }

    trigger_couplings = {"DC": 0, "AC": 1, "LF": 2, "HF": 3}
    scope_channels = {"A": 1, "B": 2, "C": 3, "D": 4}

    # --- Construction / lifecycle -------------------------------------------

    def __init__(self, address: str = "192.168.111.111", port: int = 55555, timeout: float = 5.0):
        """
        Connect to the device.

        If the connection fails, `self.socket` will be None.
        """
        self.socket: Optional[socket.socket] = None
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.settimeout(timeout)
            s.connect((address, port))
            self.socket = s
        except OSError:
            self.socket = None

    def close(self) -> None:
        """Close the TCP socket (safe to call multiple times)."""
        if self.socket is not None:
            try:
                self.socket.close()
            finally:
                self.socket = None

    # --- Low-level transport helpers ----------------------------------------

    def _require_socket(self) -> None:
        if self.socket is None:
            raise MeasurementDeviceError("Socket is not connected.")

    def _send(self, data: bytes) -> None:
        """Send raw bytes to the device."""
        self._require_socket()
        try:
            self.socket.sendall(data)
        except OSError as e:
            raise MeasurementDeviceError(f"Socket send failed: {e}") from e

    def _recv_exact(self, n: int) -> Optional[bytes]:
        """
        Receive exactly `n` bytes.

        Returns None on timeout.
        Raises MeasurementDeviceError if the socket closes unexpectedly.
        """
        self._require_socket()
        buf = bytearray()
        while len(buf) < n:
            try:
                chunk = self.socket.recv(n - len(buf))
            except socket.timeout:
                return None
            except OSError as e:
                raise MeasurementDeviceError("Socket closed.") from e

            if not chunk:
                raise MeasurementDeviceError("Socket closed.")
            buf.extend(chunk)
        return bytes(buf)

    def _recv(self) -> Optional[bytes]:
        """
        Receive one full device message.

        The protocol is:
        - 2 bytes length prefix (word count)
        - total message length = word_count * 2 bytes
        """
        hdr = self._recv_exact(2)
        if hdr is None:
            return None

        word_count = (hdr[0] << 8) | hdr[1]
        total_len = word_count * 2

        # The total length includes the 2-byte length field itself.
        rest = self._recv_exact(total_len - 2)
        if rest is None:
            return None

        return hdr + rest

    def _send_recv(self, data: bytes) -> Optional[bytes]:
        """
        Send a request and wait for a response with the same message id.

        The message id is stored at bytes [4:6] (big-endian).
        Returns None on timeout.
        """
        sent_msg_id = (data[4] << 8) | data[5]
        self._send(data)

        last = None
        for _ in range(1, 100):
            received = self._recv()
            if received is None:
                return None

            last = received
            recv_msg_id = (received[4] << 8) | received[5]
            if sent_msg_id == recv_msg_id:
                return received

        # Fallback: return last received frame (should not normally happen).
        return last

    # --- Binary parsing helpers ---------------------------------------------

    @staticmethod
    def _u16(raw: bytes, offset: int) -> int:
        return struct.unpack(">H", raw[offset: offset + 2])[0]

    @staticmethod
    def _i16(raw: bytes, offset: int) -> int:
        return struct.unpack(">h", raw[offset: offset + 2])[0]

    @staticmethod
    def _u32(raw: bytes, offset: int) -> int:
        return struct.unpack(">I", raw[offset: offset + 4])[0]

    @staticmethod
    def _i32(raw: bytes, offset: int) -> int:
        return struct.unpack(">i", raw[offset: offset + 4])[0]

    def _status(self, raw: bytes) -> int:
        """Device status word (0 means success)."""
        return self._u16(raw, 12)

    @staticmethod
    def _mk_msg_u16(payload_u16: list[int], extra_bytes: bytes = b"") -> bytes:
        """
        Build a message consisting of a 2-byte word-count prefix plus:
        - big-endian uint16 payload values
        - optional trailing bytes
        """
        body = b"".join(struct.pack(">H", x & 0xFFFF) for x in payload_u16) + extra_bytes
        word_count_plus_1 = (len(body) // 2) + 1
        return struct.pack(">H", word_count_plus_1) + body

    # --- Service commands ----------------------------------------------------

    def ServiceGetInfo(self) -> Dict[str, Any]:
        """
        Request device identification and calibration metadata.

        Returns a dictionary of version strings and identifiers.
        """
        payload = [0x0000, 0x0030, 0x0008, 0x0030, 0x0015, 0x0000]
        msg = self._mk_msg_u16(payload, extra_bytes=b"\x00\x00" * 14)

        raw = self._send_recv(msg)
        if raw is None:
            raise MeasurementDeviceError("Timeout waiting for response.")

        status = self._status(raw)
        if status != 0:
            raise MeasurementDeviceError(f"Device returned error {status:04X}.")

        return {
            "FWVersion": f"{raw[14]}.{raw[15]}",
            "LDVersion": f"{raw[16]}.{raw[17]}",
            "Serial": self._u16(raw, 18),
            "TPUVersion": f"{raw[20]}.{raw[21]}",
            "FPGAVersion": f"{raw[22]}.{raw[23]}",
            "EEPROMVersion": f"{raw[24]}.{raw[25]}",
            "Production": f"{raw[29]}.{raw[28]}.{self._u16(raw, 26)}",
            "Calibration": f"{raw[33]}.{raw[32]}.{self._u16(raw, 30)}",
            "CalibratedBy": self._u16(raw, 34),
            "PSVersion": f"{raw[36]}.{raw[37]}",
            "PSLDVersion": f"{raw[38]}.{raw[39]}",
            "PSSerial": self._u16(raw, 40),
        }

    def ServiceGetHWVersion(self) -> Dict[str, Any]:
        """
        Request hardware revision versions (mainboard, power supply, etc.).

        Returns a dictionary of version strings.
        """
        payload = [0x0000, 0x0060, 0x0008, 0x0060, 0x0015, 0x0000]
        msg = self._mk_msg_u16(payload, extra_bytes=b"\x00\x00" * 6)

        raw = self._send_recv(msg)
        if raw is None:
            raise MeasurementDeviceError("Timeout waiting for response.")

        status = self._status(raw)
        if status != 0:
            raise MeasurementDeviceError(f"Device returned error {status:04X}.")

        return {
            "MBVersion": f"{raw[15]}.{raw[14]}",
            "FPVersion": f"{raw[17]}.{raw[16]}",
            "PSVersion": f"{raw[19]}.{raw[18]}",
            "URDIVersion": f"{raw[21]}.{raw[20]}",
            "DSO1Version": f"{raw[23]}.{raw[22]}",
            "DSO2Version": f"{raw[25]}.{raw[24]}",
        }

    def ServiceGetConnectedSensors(self) -> Dict[str, int]:
        """
        Query which sensors are connected to DT_1 and DT_2.

        This uses the same message you captured in Wireshark (MsgId 0x003F).
        The device returns the detected DT socket sensor IDs near the end of the response.

        Returns:
            {"DT_1": <uint16 sensor_id>, "DT_2": <uint16 sensor_id>}

        Notes:
        - If a socket is empty or the sensor is not recognized by firmware, its ID is 0.
        - From your captures, the DT fields can be extracted from the last 12 bytes:
            DT_1_ID = tail[6:8] (big-endian uint16)
            DT_2_ID = tail[8:10] (big-endian uint16)
        """
        # This payload reproduces your captured request:
        # length prefix is auto-generated by _mk_msg_u16 (should become 0x0013).
        payload = [
            0x8000,  # command group / flags (as captured)
            0x003F,  # message id
            0x0008,  # service id(?) constant in your captures
            0x003F,  # message id echoed
            0x0000,
            0x0000,
            0x0001,
            0x0100,
            0x04D2,
            0x0001,
            0x0100,
            0x04D3,
            0x0000,
            0x0001,
            0x0000,
            0x0002,
            0x0006,
            0x0000,
        ]

        raw = self._send_recv(self._mk_msg_u16(payload))
        self._raise_on_timeout_or_error(raw)

        assert raw is not None  # for type-checkers
        if len(raw) < 12:
            raise MeasurementDeviceError("Malformed response (too short).")

        tail = raw[-12:] if len(raw) >= 12 else raw
        dt1_id = struct.unpack(">H", tail[6:8])[0] if len(tail) >= 10 else 0
        dt2_id = struct.unpack(">H", tail[8:10])[0] if len(tail) >= 10 else 0

        return {"DT_1": dt1_id, "DT_2": dt2_id}

    def CheckPressure30Sensor(self) -> Dict[str, Any]:
        """
        Convenience helper: check whether the 30 bar sensor (ID 0x08 / DRUCK_30) is connected.

        Returns:
            {
              "DT_1": bool,
              "DT_2": bool,
              "any": bool,
              "DT_1_ID": int,
              "DT_2_ID": int
            }
        """
        ids = self.ServiceGetConnectedSensors()
        target = int(self.sensors["DRUCK_30"]) & 0xFFFF

        on_dt1 = ids["DT_1"] == target
        on_dt2 = ids["DT_2"] == target
        return {
            "DT_1": on_dt1,
            "DT_2": on_dt2,
            "any": on_dt1 or on_dt2,
            "DT_1_ID": ids["DT_1"],
            "DT_2_ID": ids["DT_2"],
        }

    # --- URDI / DSO single-value measurement commands -----------------------

    def URDI_SetRange(self, socket_name: str, sensor: str, range_name: str, coupling: str, mode: str) -> None:
        """Configure a URDI input range."""
        self._validate_params(socket_name, sensor, range_name, coupling, mode)

        payload = [
            0x0000, 0x0033, 0x0006, 0x0033, 0x0015, 0x0000,
            self.sockets[socket_name],
            self.sensors[sensor],
            self.ranges[range_name],
            self.couplings[coupling],
            self.modes[mode],
        ]
        raw = self._send_recv(self._mk_msg_u16(payload))
        self._raise_on_timeout_or_error(raw)

    def URDI_Prepare(self, socket_name: str, sensor: str, range_name: str, coupling: str, mode: str) -> None:
        """Prepare the URDI subsystem for value acquisition."""
        self._validate_params(socket_name, sensor, range_name, coupling, mode)

        payload = [
            0x0000, 0x003E, 0x0006, 0x003E, 0x0015, 0x0000,
            self.sockets[socket_name],
            self.sensors[sensor],
            self.ranges[range_name],
            self.couplings[coupling],
            self.modes[mode],
        ]
        raw = self._send_recv(self._mk_msg_u16(payload))
        self._raise_on_timeout_or_error(raw)

    def URDI_GetValue(self, socket_name: str, sensor: str, range_name: str, coupling: str, mode: str) -> Dict[str, Any]:
        """
        Read a URDI value (float64).

        Note: the device may return status 0x1008, which is treated as non-fatal
        (as in the original implementation).
        """
        self._validate_params(socket_name, sensor, range_name, coupling, mode)

        payload = [
            0x0000, 0x0031, 0x0006, 0x0031, 0x0015, 0x0000,
            self.sockets[socket_name],
            self.sensors[sensor],
            self.ranges[range_name],
            self.couplings[coupling],
            self.modes[mode],
        ]
        raw = self._send_recv(self._mk_msg_u16(payload, extra_bytes=b"\x00" * 8))
        if raw is None:
            raise MeasurementDeviceError("Timeout waiting for response.")

        status = self._status(raw)
        if status not in (0x0000, 0x1008):
            raise MeasurementDeviceError(f"Device returned error {status:04X}.")

        value = struct.unpack(">d", raw[24:32])[0]
        return {"Value": value, "ErrorCode": status}

    def URDI_Finish(self) -> None:
        """Finish/cleanup URDI operations."""
        payload = [0x0000, 0x003D, 0x0006, 0x003D, 0x0015, 0x0000]
        raw = self._send_recv(self._mk_msg_u16(payload))
        self._raise_on_timeout_or_error(raw)

    def URDI_UseCalibData(self) -> None:
        """Enable use of calibration data inside the device."""
        payload = [0x0000, 0x0035, 0x0006, 0x0035, 0x0015, 0x0000, 0x0001]
        raw = self._send_recv(self._mk_msg_u16(payload))
        self._raise_on_timeout_or_error(raw)

    def URDI_NoCalibData(self) -> None:
        """Disable use of calibration data inside the device."""
        payload = [0x0000, 0x0035, 0x0006, 0x0035, 0x0015, 0x0000, 0x0000]
        raw = self._send_recv(self._mk_msg_u16(payload))
        self._raise_on_timeout_or_error(raw)

    def DSO_Prepare(self, socket_name: str, sensor: str, range_name: str, coupling: str, mode: str) -> None:
        """Prepare the DSO subsystem for value acquisition."""
        self._validate_params(socket_name, sensor, range_name, coupling, mode)

        payload = [
            0x0000, 0x003E, 0x0005, 0x003E, 0x0015, 0x0000,
            self.sockets[socket_name],
            self.sensors[sensor],
            self.ranges[range_name],
            self.couplings[coupling],
            self.modes[mode],
        ]
        raw = self._send_recv(self._mk_msg_u16(payload))
        self._raise_on_timeout_or_error(raw)

    def DSO_GetValue(self, socket_name: str, sensor: str, range_name: str, coupling: str, mode: str) -> Dict[str, Any]:
        """Read a DSO value (float64)."""
        self._validate_params(socket_name, sensor, range_name, coupling, mode)

        payload = [
            0x0000, 0x0031, 0x0005, 0x0031, 0x0015, 0x0000,
            self.sockets[socket_name],
            self.sensors[sensor],
            self.ranges[range_name],
            self.couplings[coupling],
            self.modes[mode],
        ]
        raw = self._send_recv(self._mk_msg_u16(payload, extra_bytes=b"\x00" * 8))
        self._raise_on_timeout_or_error(raw)

        value = struct.unpack(">d", raw[24:32])[0]
        return {"Value": value}

    def DSO_Finish(self) -> None:
        """Finish/cleanup DSO operations."""
        payload = [0x0000, 0x003D, 0x0005, 0x003D, 0x0015, 0x0000]
        raw = self._send_recv(self._mk_msg_u16(payload))
        self._raise_on_timeout_or_error(raw)

    # --- Scope / acquisition commands ---------------------------------------

    def Scope_Start(self) -> None:
        """
        Start scope acquisition.

        The device may return 0x2026 which is treated as non-fatal (original behavior).
        """
        payload = [0x0000, 0x003A, 0x0003, 0x003A, 0x0015, 0x0000]
        raw = self._send_recv(self._mk_msg_u16(payload))
        if raw is None:
            raise MeasurementDeviceError("Timeout waiting for response.")
        status = self._status(raw)
        if status not in (0x0000, 0x2026):
            raise MeasurementDeviceError(f"Device returned error {status:04X}.")

    def Scope_Stop(self) -> None:
        """Stop scope acquisition."""
        payload = [0x0000, 0x003B, 0x0003, 0x003B, 0x0015, 0x0000]
        raw = self._send_recv(self._mk_msg_u16(payload))
        self._raise_on_timeout_or_error(raw)

    def Scope_Finish(self) -> None:
        """Finish/cleanup scope operations."""
        payload = [0x0000, 0x003D, 0x0003, 0x003D, 0x0015, 0x0000]
        raw = self._send_recv(self._mk_msg_u16(payload))
        self._raise_on_timeout_or_error(raw)

    def Scope_SetChannel(
        self,
        channel: str,
        socket_name: str,
        sensor: str,
        range_name: str,
        coupling: str,
        filter_name: str,
        mode: str,
    ) -> None:
        """Configure a scope channel input source and conditioning."""
        if channel not in self.scope_channels:
            raise MeasurementDeviceError("Wrong parameter!")
        self._validate_params(socket_name, sensor, range_name, coupling, mode)
        if filter_name not in self.scope_filters:
            raise MeasurementDeviceError("Wrong parameter!")

        payload = [
            0x0000, 0x0030, 0x0003, 0x0030, 0x0015, 0x0000,
            self.scope_channels[channel],
            self.sockets[socket_name],
            self.sensors[sensor],
            self.ranges[range_name],
            self.couplings[coupling],
            self.scope_filters[filter_name],
            self.modes[mode],
        ]
        raw = self._send_recv(self._mk_msg_u16(payload))
        self._raise_on_timeout_or_error(raw)

    def Scope_SetTrigger(
        self,
        socket_name: str,
        sensor: str,
        range_name: str,
        coupling: str,
        filter_name: str,
        mode: str,
        trigger_mode: str,
        trigger_edge: str,
        trigger_level: int,
        trigger_timeout: int,
        precount: int,
    ) -> None:
        """Configure the trigger (short precount format)."""
        self._validate_params(socket_name, sensor, range_name, coupling, mode)
        if filter_name not in self.trigger_filters or trigger_mode not in self.trigger_modes or trigger_edge not in self.trigger_edges:
            raise MeasurementDeviceError("Wrong parameter!")

        # This command packs most fields as signed int16 (per original implementation).
        fields = [
            0x0000, 0x0031, 0x0003, 0x0031, 0x0015, 0x0000,
            self.sockets[socket_name],
            self.sensors[sensor],
            self.ranges[range_name],
            self.couplings[coupling],
            self.trigger_filters[filter_name],
            self.modes[mode],
            self.trigger_modes[trigger_mode],
            self.trigger_edges[trigger_edge],
            int(trigger_level),
            int(trigger_timeout),
            int(precount),
        ]
        body = b"".join(struct.pack(">h", int(x)) for x in fields)
        msg = struct.pack(">H", (len(body) // 2) + 1) + body

        raw = self._send_recv(msg)
        self._raise_on_timeout_or_error(raw)

    def Scope_SetTriggerLong(
        self,
        socket_name: str,
        sensor: str,
        range_name: str,
        coupling: str,
        filter_name: str,
        mode: str,
        trigger_mode: str,
        trigger_edge: str,
        trigger_level: int,
        trigger_timeout: int,
        precount: int,
    ) -> None:
        """Configure the trigger (32-bit precount format)."""
        self._validate_params(socket_name, sensor, range_name, coupling, mode)
        if filter_name not in self.trigger_filters or trigger_mode not in self.trigger_modes or trigger_edge not in self.trigger_edges:
            raise MeasurementDeviceError("Wrong parameter!")

        fields = [
            0x0000, 0xA031, 0x0003, 0xA031, 0x0015, 0x0000,
            self.sockets[socket_name],
            self.sensors[sensor],
            self.ranges[range_name],
            self.couplings[coupling],
            self.trigger_filters[filter_name],
            self.modes[mode],
            self.trigger_modes[trigger_mode],
            self.trigger_edges[trigger_edge],
            int(trigger_level),
            int(trigger_timeout),
            0,
        ]
        body = b"".join(struct.pack(">h", int(x)) for x in fields) + struct.pack(">l", int(precount))
        msg = struct.pack(">H", (len(body) // 2) + 1) + body

        raw = self._send_recv(msg)
        self._raise_on_timeout_or_error(raw)

    def Scope_Prepare(self, sample_rate: str, sample_method: str, count: int) -> None:
        """Prepare scope capture (16-bit sample count)."""
        if sample_rate not in self.scope_sample_rates or sample_method not in self.scope_sample_methods:
            raise MeasurementDeviceError("Wrong parameter!")

        payload = [
            0x0000, 0x003E, 0x0003, 0x003E, 0x0015, 0x0000,
            self.scope_sample_rates[sample_rate],
            self.scope_sample_methods[sample_method],
            int(count) & 0xFFFF,
        ]
        raw = self._send_recv(self._mk_msg_u16(payload))
        self._raise_on_timeout_or_error(raw)

    def Scope_PrepareLong(self, sample_rate: str, sample_method: str, count: int) -> None:
        """Prepare scope capture (32-bit sample count)."""
        if sample_rate not in self.scope_sample_rates or sample_method not in self.scope_sample_methods:
            raise MeasurementDeviceError("Wrong parameter!")

        payload = [
            0x0000, 0xA03E, 0x0003, 0xA03E, 0x0015, 0x0000,
            self.scope_sample_rates[sample_rate],
            self.scope_sample_methods[sample_method],
            0,
        ]
        body = b"".join(struct.pack(">H", x & 0xFFFF) for x in payload) + struct.pack(">L", int(count))
        msg = struct.pack(">H", (len(body) // 2) + 1) + body

        raw = self._send_recv(msg)
        self._raise_on_timeout_or_error(raw)

    def Scope_ReceiveData(self) -> Optional[Dict[str, Any]]:
        """
        Receive scope data in the "short header" format.

        Returns:
            A dictionary with metadata and raw sample bytes in `Data`, or None on timeout.

        The `Data` field contains packed samples. The sample size depends on SampleMethod:
        - AVERAGE -> 2 bytes/sample
        - others  -> 4 bytes/sample
        """
        raw = self._recv_until_msg_id(0x0090)
        if raw is None:
            return None

        header_size = 52
        parsed = self._parse_scope_header_short(raw)

        sample_size = 2 if parsed["SampleMethod"] == self.scope_sample_methods["AVERAGE"] else 4
        data = bytearray()
        data.extend(raw[header_size:])

        # Some frames are fragmented. Continuation frames use msg id 0xA090.
        while (len(data) // sample_size) < parsed["Count"]:
            frag = self._recv_until_msg_id(0xA090)
            if frag is None:
                return None
            data.extend(frag[header_size:])

        parsed["Data"] = bytes(data)
        return parsed

    def Scope_ReceiveDataLong(self) -> Optional[Dict[str, Any]]:
        """
        Receive scope data in the "long header" format (includes fragment metadata and 32-bit counts).

        Returns:
            A dictionary with metadata and raw sample bytes in `Data`, or None on timeout.
        """
        raw = self._recv_until_msg_id(0xA090)
        if raw is None:
            return None

        header_size = 62
        parsed = self._parse_scope_header_long(raw)

        sample_size = 2 if parsed["SampleMethod"] == self.scope_sample_methods["AVERAGE"] else 4
        data = bytearray()
        data.extend(raw[header_size:])

        while (len(data) // sample_size) < parsed["Count"]:
            frag = self._recv_until_msg_id(0xA090)
            if frag is None:
                return None
            data.extend(frag[header_size:])

        parsed["Data"] = bytes(data)
        return parsed

    # --- Internal helpers for parameter validation and parsing ---------------

    def _validate_params(self, socket_name: str, sensor: str, range_name: str, coupling: str, mode: str) -> None:
        if (
            socket_name not in self.sockets
            or sensor not in self.sensors
            or range_name not in self.ranges
            or coupling not in self.couplings
            or mode not in self.modes
        ):
            raise MeasurementDeviceError("Wrong parameter!")

    def _raise_on_timeout_or_error(self, raw: Optional[bytes]) -> None:
        if raw is None:
            raise MeasurementDeviceError("Timeout waiting for response.")
        status = self._status(raw)
        if status != 0:
            raise MeasurementDeviceError(f"Device returned error {status:04X}.")

    def _recv_until_msg_id(self, wanted_msg_id: int) -> Optional[bytes]:
        """
        Read incoming frames until a frame with the given message id arrives.
        Returns None on timeout.
        """
        for _ in range(1, 100):
            raw = self._recv()
            if raw is None:
                return None
            msg_id = (raw[4] << 8) | raw[5]
            if msg_id == wanted_msg_id:
                return raw
        return None

    def _parse_scope_header_short(self, raw: bytes) -> Dict[str, Any]:
        """Parse the 52-byte scope header used by Scope_ReceiveData()."""
        return {
            "SeqNumber": (raw[16] << 24) + (raw[17] << 16) + (raw[14] << 8) + raw[15],
            "ChannelNo": self._u16(raw, 18),
            "SocketId": self._u16(raw, 20),
            "SensorID": self._u16(raw, 22),
            "SampleRateId": self._u16(raw, 24),
            "SampleRateInternalId": self._u16(raw, 26),
            "InputRangeId": self._u16(raw, 28),
            "Count": self._u16(raw, 30),
            "PreCount": self._i16(raw, 32),
            "TriggerLevel": self._u16(raw, 34),
            "TriggerRangeId": self._u16(raw, 36),
            "TriggerCoupling": self._u16(raw, 38),
            "TriggerUsed": self._u16(raw, 40),
            "SampleMethod": self._u16(raw, 42),
            "CalOffset": self._i16(raw, 44),
            "CalGain": self._i16(raw, 46),
            "CalcOffsetScopeChannel": self._i16(raw, 48),
            "CalcGainScopeChannel": self._i16(raw, 50),
        }

    def _parse_scope_header_long(self, raw: bytes) -> Dict[str, Any]:
        """Parse the 62-byte scope header used by Scope_ReceiveDataLong()."""
        return {
            "SeqNumber": (raw[16] << 24) + (raw[17] << 16) + (raw[14] << 8) + raw[15],
            "ChannelNo": self._u16(raw, 18),
            "SocketId": self._u16(raw, 20),
            "SensorID": self._u16(raw, 22),
            "SampleRateId": self._u16(raw, 24),
            "SampleRateInternalId": self._u16(raw, 26),
            "InputRangeId": self._u16(raw, 28),
            "TotalFragmentCount": self._u16(raw, 30),
            "CurrentFragmentNumber": self._u16(raw, 32),
            "FragmentFlags": self._u16(raw, 34),
            "Count": self._u32(raw, 36),
            "PreCount": self._i32(raw, 40),
            "TriggerLevel": self._u16(raw, 44),
            "TriggerRangeId": self._u16(raw, 46),
            "TriggerCoupling": self._u16(raw, 48),
            "TriggerUsed": self._u16(raw, 50),
            "SampleMethod": self._u16(raw, 52),
            "CalOffset": self._i16(raw, 54),
            "CalGain": self._i16(raw, 56),
            "CalcOffsetScopeChannel": self._i16(raw, 58),
            "CalcGainScopeChannel": self._i16(raw, 60),
        }