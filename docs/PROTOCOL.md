# The DualShock 4 Bluetooth speaker protocol

This is everything `padspeaker/protocol.py` and `padspeaker/sbc.py` implement, at byte level. Nothing here is new: it
comes from the public reverse-engineering work listed at the end. What this project adds is the checking: every number
below is covered by a test, and the audio path was heard on real hardware (see "How it was verified").

Applies to the DualShock 4 (v1 and v2, HID product ids `054C:05C4` and `054C:09CC`, and the Sony USB dongle id
`054C:0BA0`) and to clones that speak the same Bluetooth report format. It does not apply to the DualSense.

## Overview

Over USB a DS4 shows up as a USB sound card. Over Bluetooth it is only a HID device, but it still accepts audio: a
HID **output report** can carry compressed audio for the built-in speaker. To play sound you write two kinds of output
report to the pad's `/dev/hidrawN`:

- **report `0x17`** (462 bytes): four SBC audio frames, one report every 16 ms;
- **report `0x11`** (78 bytes): controller state, used here for the speaker volume and the lightbar colour.

Writing a report is one `write()` of the complete report (report id first, CRC last). Nothing else is needed: no
pairing step, no feature report, no handshake. Timing is the sender's job.

## The audio stream

| property | value |
|---|---|
| sample rate | 32 000 Hz |
| channels | 2 (stereo, sent as SBC "dual channel") |
| PCM format fed to the encoder | signed 16 bit little endian |
| codec | SBC (libsbc), dual channel, 16 blocks, 8 subbands, loudness allocation, bitpool 25 |
| bytes of PCM per SBC frame | 512 (128 samples x 2 channels x 2 bytes) |
| SBC frame size | 112 bytes, always starting with the bytes `9c 75 19` |
| duration of one frame | 4 ms (128 samples at 32 kHz) |
| frames per report | 4, so 16 ms of audio per report |

The three header bytes are the SBC syncword `9c`, the configuration byte `75` (32 kHz, 16 blocks, dual channel,
loudness, 8 subbands) and the bitpool `19` (25). A frame that does not start with them was not produced with the right
settings, which is why `SbcEncoder.encode()` checks it.

SBC keeps filter history between frames, so use one encoder per stream and feed it consecutive chunks. ffmpeg's `sbc`
encoder cannot select dual channel mode, so libsbc is used directly (through `ctypes`).

## Audio output report `0x17` (462 bytes)

| offset | size | value | meaning |
|---|---|---|---|
| 0 | 1 | `0x17` | report id |
| 1 | 1 | `0x40` | CRC enabled (the report ends with a CRC-32) |
| 2 | 1 | `0xA0` | audio enabled; bit 5 is always set; the low 3 bits (microphone enable) are kept at 0 |
| 3 to 4 | 2 | counter | frame counter, little endian, +4 per report (one per SBC frame), wraps at 16 bits |
| 5 | 1 | `0x02` | audio target: `0x02` speaker, `0x24` headset jack |
| 6 to 453 | 448 | 4 x 112 | four SBC frames back to back |
| 454 to 457 | 4 | `00 00 00 00` | zero padding |
| 458 to 461 | 4 | CRC-32 | little endian, see below |

Reports are sent every 16 ms. The pad plays at a fixed rate, so the sender has to keep to real time: much faster fills
the pad's buffer, slower leaves gaps. When there is no audio the sender should send silence (or stop sending), never
replay old audio.

## State output report `0x11` (78 bytes)

| offset | size | value | meaning |
|---|---|---|---|
| 0 | 1 | `0x11` | report id |
| 1 | 1 | `0xC0` | flags: `0x80` HID and `0x40` CRC enabled; the low bits (poll rate) are left at 0 |
| 2 | 1 | `0x20` | state update |
| 3 to 73 | 71 | state block | see next table (offsets there are relative to byte 3) |
| 74 to 77 | 4 | CRC-32 | little endian |

State block (the 71 bytes starting at report offset 3):

| block byte | report offset | meaning |
|---|---|---|
| 0 | 3 | update flags: `0x02` lightbar, `0x80` speaker volume, `0x10` and `0x20` headset left and right volume |
| 5, 6, 7 | 8, 9, 10 | lightbar red, green, blue (0 to 255) |
| 21 | 24 | speaker volume, `0x00` to `0x4F` (79). Values above `0x4F` are not valid and this project clamps them |

Only the fields whose flag is set are applied by the pad. `build_state_report(volume=..., rgb=...)` sets the flags for
exactly the fields you pass. Setting the volume does not need the lightbar flag and vice versa, but see the lightbar
note below.

## The CRC

The CRC is the usual CRC-32 (the one in zlib / `binascii.crc32`) computed over the byte `0xA2` followed by the report
without its last four bytes, then stored little endian in those four bytes. `0xA2` is the HID "output report, Bluetooth"
transaction header; it is part of the checksum but is not part of what you write to hidraw.

For report `0x17` the checksummed bytes are `A2` plus offsets 0 to 457; for `0x11` they are `A2` plus offsets 0 to 73.

```python
import struct, zlib
crc = zlib.crc32(b"\xa2" + report_without_crc) & 0xFFFFFFFF
report = report_without_crc + struct.pack("<I", crc)
```

A worked example: setting the speaker volume to `0x40` gives this 78 byte report (CRC in the last four bytes):

```
11c0208000000000000000000000000000000000000000004000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000d946ea58
```

An audio report with counter 0 and four frames that are `9c 75 19` followed by 109 zero bytes starts with
`1740a00000029c75` and ends with the CRC bytes `5a ea 10 45`.

## The lightbar caveat

On the pads tested (DS4 v2 clones) the lightbar goes **off while audio reports are being sent**: audio reports carry
no colour, and the pad's own state falls back to "off". Verified on hardware: re-sending the colour in a `0x11` report
with the lightbar flag (`0x02`, bytes 5 to 7 of the state block) about every 0.5 s while audio is streamed keeps the
light on. The daemon does exactly that (setting `keep_lightbar`, on by default):

- it sends the lightbar-only report right after an audio report, in the slack before the next deadline, so it never
  delays audio (the report clock uses absolute deadlines, so the write time comes out of the following sleep);
- it keeps doing so while the sound is silent and audio reports are paused, because the pad is still in streaming mode;
- the colour is the one the kernel LED class held before streaming (`/sys/class/leds/<input>:red|green|blue/brightness`,
  written by `hid-playstation`), re-read at most once a second so a recolouring by the system is followed; if it cannot
  be read, the fallback `lightbar_fallback` (default dim blue, `0,0,64`) is used;
- a failed lightbar write is handled like any failed write: streaming stops cleanly.

When streaming ends the daemon writes the saved values back to the kernel LED files, which makes the driver send a fresh
state report, so the colour is exactly what it was before. The pad stays connected and usable throughout.

## The Bluetooth link: sniff mode

Not part of the report format, but needed for it to work for more than a few seconds. Constant 16 ms audio traffic over a
link that is in **sniff mode** (HCI link policy `RSWITCH SNIFF`, a power saving mode) made BlueZ fail to push a HID
message through: `bluetoothd` logged `hidp_send_message() BT socket write error: Resource temporarily unavailable (11)`
about 14 seconds into streaming, then the HID device vanished (`write()` to the hidraw node fails with `ENODEV` or `EIO`)
while the ACL link itself stayed up, leaving the pad "connected" but unusable. It was reproducible on one pad (a second
pad, with a link in that mode).

Fix, verified on hardware: remove `SNIFF` from the link policy with `hcitool lp <MAC> RSWITCH` (for links created later,
`hciconfig hci0 lp rswitch` sets it adapter-wide). After that, streaming to that pad ran 55 s by hand and then through the
daemon for about 26 s with no write errors, where it had failed every time before. BlueZ can set the policy again, so the
daemon applies it when a pad connects, right before it starts streaming, and every 30 s while streaming, and logs at INFO
only when it actually changed something. Check it with `hcitool lp <MAC>`: it must read `RSWITCH` without `SNIFF`.

If a write nevertheless fails with `ENODEV` or `EIO`, the daemon runs `bluetoothctl disconnect <MAC>` once (rate limited)
so that the pad is disconnected cleanly and a press of the PS button reconnects it.

## Pacing and latency

- One report per 16 ms with absolute deadlines (`t0 + n * 16 ms`), so sleep jitter does not accumulate.
- A short jitter buffer (3 reports = 48 ms by default) between the audio source and the sender absorbs scheduling
  noise. On top of that the sender runs a couple of reports ahead of real time, which fills the pad's own buffer and
  matched the proof of concept (it ran about 30 ms ahead).
- If the sender stalls for more than about 100 ms, restart the schedule rather than sending a burst to catch up.
- Underrun: send silence. Overrun: drop the oldest audio, in whole stereo samples (4 bytes).

## How it was verified

1. **On hardware.** A proof of concept (`tools/ds4_tone_poc.py`, kept only as an independent reference; use `padspeaker tone` for a hardware check) sent a 6 second
   440 Hz tone to a DS4 v2 clone (HID id `0005:054C:09CC`, Linux 6.11, `hid-playstation`, over Bluetooth) and the tone was
   clearly audible on the pad's speaker. Afterwards the lightbar was off; rewriting the kernel LED brightness restored it.
   The pad stayed connected throughout. `python3 tools/box_selftest.py --tone` and `padspeaker tone` repeat that
   test through the library code.
2. **Against the public documentation.** The layout above agrees with SensePost's Dual-Pod-Shock write-up, the PS4
   developer wiki page for the DS4 over Bluetooth, and the snipem/DS4Dongle source (links below).
3. **By tests** (`tests/test_protocol.py`, `tests/test_sbc.py`): constants and offsets of both reports; the CRC against
   a hand-computed vector (CRC-32 of `A2 11 C0 20`) and against the report the proof of concept builds, byte for byte;
   counter wrap at 16 bits; volume clamping at `0x4F`; every rejected input (wrong frame count or size, bad target).
   The SBC test asserts that one encode call turns 512 bytes of PCM into exactly 112 bytes starting `9c 75 19`; it runs
   only where libsbc is installed and is skipped elsewhere.
4. **On the target machine**, `tools/box_selftest.py` loads libsbc, encodes a frame and checks size and header before
   anything is sent to a pad.

## Sources

- SensePost, "Dual-Pod-Shock: emotional abuse of a DualShock": <https://sensepost.com/blog/2020/dual-pod-shock-emotional-abuse-of-a-dualshock>
- PS4 Developer wiki, DS4-BT (DualShock 4 over Bluetooth): <https://www.psdevwiki.com/ps4/DS4-BT>
- snipem/DS4Dongle: <https://github.com/snipem/DS4Dongle>
- DualShock 4 data structures (controllers wiki): <https://controllers.fandom.com/wiki/Sony_DualShock_4/Data_Structures>
