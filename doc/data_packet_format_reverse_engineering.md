# WaveScope Data Packet Format (DT Pressure Sensor 0x08)

This document explains how the WaveScope “scope streaming” frames are structured and how to decode the raw sample payload, including the calibration fields embedded in each frame.

It is written to match what the Python GUI receives as a `frame` dictionary (e.g. `frame["Data"]`, `frame["SampleMethod"]`, `frame["CalOffset"]`, etc.) and what the firmware implements.

---

## 1. High-level overview

A scope stream frame contains:

1. A **general message header** (transport/meta information)
2. A **scope frame header** (channel, socket, sensor IDs, sample configuration, counters, trigger info)
3. A **variable-length sample payload** (`Data`) containing packed samples

Two payload formats are used:

- **AVERAGE** sample method → **16-bit samples** (`uint16` per sample)
- Other methods (MIN, MAX, MINMAX) → **32-bit samples** (`uint32` per sample)

All multi-byte numeric fields are transmitted **big-endian**.

---

## 2. Endianness and “length” conventions

### Endianness

All numeric fields in the scope packet (and the sample payload) are **big-endian**.

- 16-bit: `>H` or `>h`
- 32-bit: `>I` or `>i`

### Message length field

The general header contains `MsgLen`, which is typically the **length in 16-bit words**, not bytes.

- `MsgLen_words * 2 = MsgLen_bytes`

---

## 3. Packet layout

The firmware defines a scope data message (conceptually).

### 3.1 General message header (always present)

A fixed header at the start of the message:

- `MsgLen` (uint16) — length in 16-bit words
- `SenderTaskID` (uint16)
- `SenderMsgID` (uint16)
- `ReceiverTaskID` (uint16)
- `ReceiverMsgID` (uint16)
- `HandlingInfo` (uint16)
- `UniqueErrorID` (uint16)

This block is **7 × uint16 = 14 bytes**.

### 3.2 Scope frame fields (scope-specific header)

Immediately after the general header follow scope-specific fields (types shown as used in firmware):

- `SequenceNumberLow` (uint16)
- `SequenceNumberHigh` (uint16)  
  → together form a 32-bit sequence counter

- `ChannelNo` (uint16) — channel identifier (e.g. A/B)
- `SocketID` (uint16) — DT socket identifier
- `SensorID` (uint16) — sensor identifier (e.g. 0x08)
- `SampleRateID` (uint16) — configured/used sampling rate
- `SampleRateIDInternal` (uint16) — internal (FPGA) sampling rate before decimation/processing
- `InputRangeID` (uint16)
- `Count` (uint16) — number of samples in the payload (from PC point of view)
- `PreCount` (int16)
- `TriggerLevel` (uint16)
- `TriggerRangeID` (uint16)
- `TriggerCoupling` (uint16)
- `TriggerUsed` (uint16)
- `SampleMethodID` (uint16) — AVERAGE / MIN / MAX / MINMAX

#### Calibration fields (embedded in each frame)

These are present in each scope data packet:

- `CalOffset` (int16) — external calibration offset
- `CalGain` (int16) — external calibration gain
- `CalcOffsetScopeChannel` (int16) — internal/channel offset
- `CalcGainScopeChannel` (int16) — internal/channel gain

Finally, a placeholder field is declared in the firmware struct:

- `ScopeData` (uint16) — start of variable payload area

In practice, this is where the payload begins and its actual length depends on `Count` and `SampleMethodID`.

### 3.3 Payload (`Data`)

The Python side typically exposes the payload bytes as:

- `frame["Data"]` — raw sample bytes (variable length)

The meaning and packing depends on `frame["SampleMethod"]` (or `SampleMethodID`).

---

## 4. Sample payload encoding

### 4.1 AVERAGE mode (16-bit words)

If `SampleMethodID == AVERAGE`:

- The payload is `Count × uint16`
- Each `uint16` contains:
  - bits 0..11: sample value (**12-bit**)
  - bits 12..15: trigger/marker flags (4 bits)

This is important: even though the container is 16-bit, the actual “ADC value” is 12-bit, and the upper 4 bits are not part of the value.

#### Bit extraction

Given one 16-bit word `u16`:

- `value12 = u16 & 0x0FFF`
- `marks = (u16 >> 12) & 0x000F`

**Recommendation:** decode as `uint16` first.  
If you decode as `int16`, marker bits can flip the sign and create misleading negative values.

**Important for statistics:** marker samples (`marks != 0`) can appear as outliers in min/max. If you want stable min/max/avg of the actual signal, filter to `marks == 0`.

---

### 4.2 MIN / MAX / MINMAX (32-bit words)

If `SampleMethodID` is not AVERAGE (e.g. MIN, MAX, MINMAX):

- The payload is `Count × uint32`
- The firmware often stores these as two consecutive `uint16` words per sample (implementation detail), but from the network payload perspective it is **4 bytes per sample**.

In many FPGA/embedded scope implementations, the `uint32` is used to store two 16-bit values, often:

- lower 16 bits = MIN
- upper 16 bits = MAX

This is a strong working hypothesis that you can confirm experimentally:

- In MINMAX, check whether `low16 <= high16` most of the time.
- In MIN, check whether only one half changes meaningfully.
- In MAX, check whether the other half changes meaningfully.

#### Splitting a 32-bit word

Given `u32`:

- `low16 = u32 & 0xFFFF`
- `high16 = (u32 >> 16) & 0xFFFF`

Then you can optionally apply the same 12-bit + marker-bit split on each 16-bit half if the firmware/FPGA uses marker bits there as well.

---

## 5. Calibration fields and how to apply them

Each scope frame includes four calibration-related fields:

- `CalOffset` → external offset
- `CalGain` → external gain
- `CalcOffsetScopeChannel` → internal/channel offset
- `CalcGainScopeChannel` → internal/channel gain

### 5.1 What the documentation says (generic conversion)

The tasks/messages documentation contains a generic linear correction form (for scope channels):

> `data(corr.) = (gain/10000+1) * data + offset` :contentReference[oaicite:0]{index=0}

This line describes the **shape** of the correction (gain + offset).

### 5.2 What the firmware actually does (important details)

When we searched the firmware source (`src.zip`) for `fullGain`, `fullOffset`, and for how calibration offsets are applied, we found:

1) **Full gain/offset composition uses /100000 scaling and multiplies internal+external gain:**

- File: `src/sources/scope_autorange.c`  
  The firmware computes:
  - `fullGain  = (1 + intGainDC / 100000.0) * (1 + extGainDC / 100000.0);`
  - `fullOffset = fullGain * intOffsetDC + extOffsetDC;`

2) **Offset values are not used as “raw counts” — they are applied as a fraction of the full range:**

- File: `src/sources/dmm_task.c`  
  When applying external calibration to a value in physical units, the firmware uses:
  - `result = result * (1 + calib_gain / 100000.0) + (calib_offset * full_range / 100000.0);`

- File: `src/sources/scope_utilities.c`  
  A similar pattern is used in scope utilities (e.g. zero point correction):
  - `... + (offset * ranges[i].full_range / 100000.0);`

**Key conclusion:**  
In this firmware, **gain is ppm-scaled** (`/100000`) and **offset is also ppm-scaled but represents a fraction of `full_range`**, not “ADC counts”.

This is exactly why applying `fullOffset` directly as `corrected = fullGain * raw + fullOffset` can look wildly wrong if `raw` is in ADC-code units. The firmware’s semantics imply:

- Gain: multiplicative factor `1 + gain/100000`
- Offset: additive term `offset_ppm/100000 * full_range`

### 5.3 Recommended application for scope AVERAGE samples

Because scope `AVERAGE` gives you a 12-bit code (`raw12`) and you often convert it to volts first, a robust and firmware-consistent way is:

1) **Extract value bits and filter markers:**
- `raw12 = u16 & 0x0FFF`
- ignore samples with `marks != 0` if you want stable averages

2) **Convert code to volts (offset-binary assumption):**
- `V = (raw12 - 2048) / 2048.0 * V_FS`

Where `V_FS` is the effective full-scale in volts used by this channel (often 1.0 V in the DT current/pressure front-end).

3) **Compute fullGain/fullOffset (firmware style):**
- `fullGain = (1 + intGain/100000) * (1 + extGain/100000)`
- `fullOffset_ppm = fullGain * intOffset + extOffset`

4) **Apply calibration in the voltage domain using the firmware offset semantics:**
- `V_corr = V * fullGain + (fullOffset_ppm / 100000.0) * V_FS`

This mirrors the firmware’s pattern:
- multiplicative correction to the measured value
- additive correction proportional to the channel full range

### 5.4 Why “fullOffset” often looks wrong if added to raw12 directly

If you do:

- `raw12_corr = fullGain * raw12 + fullOffset`

you implicitly treat `fullOffset` as “counts”. But firmware semantics suggest it behaves like “ppm of full range”. In a typical case:

- `fullOffset ≈ 310` → `310/100000 * 1.0 V = 0.00310 V`
- Across a 49.9 Ω shunt this is about `0.00310/49.9*1000 ≈ 0.062 mA`

That is small and plausible.

But `+310 counts` would be a massive step and will not match expectations.

---

## 6. Practical decoding guidelines (Python)

### 6.1 AVERAGE decoding (recommended)

Decode as unsigned 16-bit words, then split:

- `u16 = struct.unpack(">H", ...)`
- `value12 = u16 & 0x0FFF`
- `marks = u16 >> 12`

Keep `marks` if you want to visualize trigger markers or annotate the plot.

### 6.2 Non-AVERAGE decoding

Decode as unsigned 32-bit words:

- `u32 = struct.unpack(">I", ...)`
- `lo = u32 & 0xFFFF`
- `hi = (u32 >> 16) & 0xFFFF`

Then interpret `(lo, hi)` according to mode:

- MINMAX likely (min, max)
- MIN likely uses one side
- MAX likely uses the other side

### 6.3 Do not assume “16 valid bits”

Even though AVERAGE uses 16-bit containers, the firmware documents only 12 data bits and 4 marker bits. So it is neither “true 16-bit” nor “14-bit” in this transport format.

---

## 7. Frame fields exposed in the GUI `frame` dict

Typical keys you can expect (names may match the device library):

- `frame["Data"]` — payload bytes
- `frame["Count"]` — number of samples (or sample groups) in the payload
- `frame["SampleMethod"]` — method ID
- `frame["CalOffset"]` — external offset
- `frame["CalGain"]` — external gain
- `frame["CalcOffsetScopeChannel"]` — internal/channel offset
- `frame["CalcGainScopeChannel"]` — internal/channel gain

These map directly to the firmware fields described above.

---

## 8. Troubleshooting and sanity checks

### 8.1 Payload length check

For a received frame:

- If AVERAGE: `len(Data)` should be `Count * 2`
- Else: `len(Data)` should be `Count * 4`

If not, treat it as a framing/streaming error or a mismatch in the meaning of `Count`.

### 8.2 MINMAX plausibility test

In MINMAX mode:

- Split each `u32` into `(low16, high16)`
- You should usually see `low16 <= high16` if it represents (min,max)

### 8.3 Avoid signed decoding for packed samples

If you decode packed 16-bit samples as signed, marker bits can generate negative values. Always decode AVERAGE payload as `uint16` and only convert to signed/centered representations after extracting the 12-bit value (if you actually need that transformation).

---

## 9. Known implementation note: logging in `_reader_worker`

If you use a debug `print(...)` block, ensure parentheses are correct. A nested `print()` inside another `print(` call will cause a syntax error and can hide real decoding problems.

---

## 10. Summary

- **AVERAGE payload:** `uint16` per sample  
  - bits 0..11 = value (12-bit)  
  - bits 12..15 = marker/trigger flags (4-bit)

- **MIN/MAX/MINMAX payload:** `uint32` per sample  
  - typically two 16-bit values packed into one 32-bit word

- **Calibration fields per frame:** extOffset/extGain + intOffset/intGain  
  - firmware uses ppm-like scaling: `1 + gain/100000`
  - composed:
    - `fullGain = (1 + intGain/100000) * (1 + extGain/100000)`
    - `fullOffset_ppm = fullGain * intOffset + extOffset`

- **Most important “gotcha”:**  
  In this firmware, offsets are applied as a fraction of full range:
  - additive term is `offset_ppm/100000 * full_range`
  - do **not** treat `fullOffset` as “ADC counts” unless you explicitly convert it into the correct domain.

This document describes the transport encoding and the calibration math as implemented by the firmware. The remaining “interpretation” detail is the exact semantic meaning of the two halves in the 32-bit sample modes, which can be confirmed with a quick MIN/MAX/MINMAX capture sanity test.