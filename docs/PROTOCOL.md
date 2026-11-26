# HID protocol (reverse engineered)

Notes on how an ROG Falchion ACE 75 HE (ASUS, VID `0B05` PID `1C7D`) exposes
per-key travel. Everything below was recovered by reading the vendor HID
collection and correlating raw reports with OS key events — no driver, no
injection, no firmware modification.

## Device

The keyboard exposes several HID collections. The interesting one:

| property | value |
|---|---|
| usage page | `0xFFC0` (vendor defined) |
| usage | `0x0A` |
| collection | `MI_02 COL03` |
| report ID | `0x03` |
| input report length | 21 bytes |
| rate | ~100 Hz, change driven |

Other collections (`0xFF00`, 65 byte reports) belong to the ASUS "Gear Link"
software; we never open them.

## Reports

```
03 7E <index> 00 <lo> <hi>      analog depth
03 71 <..>   <state>            threshold event
```

### `0x7E` — analog depth

* byte 2: key matrix index (not a HID usage, a keyboard-specific number)
* bytes 4–5: depth as uint16 little endian
* unit: **0.01 mm**, range `0..350` → 0.00–3.50 mm
* sent continuously (~100 Hz) while a key moves or is held

Matrix indexes observed on this model:

| index | key | index | key |
|---|---|---|---|
| 18 | W | 31 | A |
| 32 | S | 33 | D |
| 61 | Space | 15 | Backspace |
| 59 | LShift | 60 | Alt |
| 35–39 | G H J K L | 3, 4 | 2, 3 |

### `0x71` — threshold event

Byte 3 is `0x10` on press and `0x00` on release. It carries **no key index**, so
it is only useful as a global "something moved" hint.

## Firmware quirks that matter

1. **One key at a time.** The firmware streams the depth of a single active key.
   When another key becomes active the first key's stream stops, and the
   previous key is reported once as `0.00 mm`.
2. **That zero is not a release.** Distinguishing "firmware switched away" from
   "user let go" requires the OS key state (`GetAsyncKeyState`): if the key is
   still physically down, the value is kept and re-emitted every 50 ms so the
   recording still contains simultaneous keys (`Engine.run_hold`).
3. **Reports stop when idle.** After a key is released the firmware goes quiet;
   the reader therefore has to treat silence as "no change", not "disconnect".
4. **Re-enumeration.** Opening the ASUS software (or replugging) re-enumerates
   the device and the `0x7E` stream may disappear until the keyboard is
   replugged. `depth-live.py` reconnects automatically but cannot fix firmware
   state.

## Reading it from Python

`hid-probe.py` uses `ctypes` + SetupAPI directly:

```
SetupDiGetClassDevsW(HID)  ->  SetupDiEnumDeviceInterfaces
HidD_GetAttributes         ->  match VID/PID
HidP_GetCaps               ->  find usage page 0xFFC0, input length 21
CreateFileW(0, FILE_SHARE_READ|WRITE, ...)   # zero access rights
ReadFile(h, buf, 21, ...)  ->  parse
CancelIoEx                 ->  stop a blocked read
```

Note that `CreateFileW` with zero access rights is deliberate: it avoids
competing with the vendor software, but then `IOCTL_HID_GET_REPORT_DESCRIPTOR`
fails with `ERROR_INVALID_FUNCTION`. The report layout above was reconstructed
from captured traffic instead of the descriptor.

No global keyboard hook is installed: `depth-live.py` polls `GetAsyncKeyState`
every 4 ms, which anti-cheat software tolerates much better than a hook. Do not
run the monitor while a competitive game is open if you care about it.
