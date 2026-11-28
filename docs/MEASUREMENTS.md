# Measurements: the Bluetooth audio ceiling and muting the pad speaker

This page collects measurements taken on one DualShock 4 v2 over Bluetooth, on Linux with PipeWire,
while investigating two questions:

1. **How much audio can the pad actually receive over Bluetooth?** The report format fixes the answer,
   and it is lower than "CD quality".
2. **Can the pad's speaker be silenced while its headset jack keeps playing?** On the unit tested, the
   control bytes that should do this are ignored, so a signal-level trick was measured instead.

Everything here is **measurement on a single unit**, not a specification. Values that are exact come
from the protocol or from `libsbc`; values that are approximate come from a microphone and are marked
as such.

## Scope and method

- Pad: DualShock 4 v2 over Bluetooth (USB id `054C:09CC`), `hid-playstation` driver.
- Host: Linux with PipeWire; `padspeaker` sending the audio reports.
- Tones: sine waves sent through the normal streamer path, one frequency at a time.
- Residual measurements: the `padspeaker` service was stopped so its own reports could not overwrite
  the tone, a **dummy 3.5 mm plug** (a plug with no transducer) was left in the headset jack so the
  jack made no sound, and the remaining acoustic output — the pad's speaker — was recorded with a
  microphone for about one second per frequency.
- The microphone and room were not calibrated, so the residual figures are indicative, not
  laboratory-grade.

## The transport ceiling

### Report layout

Audio travels in HID output report `0x17`, sent **every 16 ms**:

| Bytes | Content |
| --- | --- |
| 0 | report id `0x17` |
| 1 | `0x40` (CRC enabled) |
| 2 | `0xA0` (audio enabled) |
| 3–4 | frame counter, little endian, +4 per report |
| 5 | target (`0x02` speaker, `0x24` headset, `0x00` off) |
| 6–453 | **448 bytes: four SBC frames of 112 bytes** |
| 454–457 | zero |
| 458–461 | CRC-32 |

So one report carries at most **448 bytes of encoded audio**, and it is sent 16 ms apart:

```
448 bytes / 0.016 s = 28 000 bytes/s = 224 000 bit/s  (~224 kbit/s)
```

That is the hard ceiling for this report format. It is split across both channels: about
112 kbit/s per channel.

### SBC parameters

The pad expects SBC with these settings:

| Parameter | Value |
| --- | --- |
| Sample rate | 32 kHz |
| Blocks | 16 |
| Subbands | 8 |
| Channel mode | dual channel |
| Allocation | loudness |
| Bitpool | 25 |
| Samples per frame | 128 (4 ms at 32 kHz) |
| PCM per frame | 512 bytes (128 samples x 2 channels x 2 bytes) |
| Frame size | 112 bytes |
| Header | `9c 75 19` |

Four frames of 128 samples cover 512 samples = 16 ms at 32 kHz, which matches the report cadence
exactly. At ~224 kbit/s over 32 kHz stereo, the codec has about **3.5 bits per sample**.

### Why bitpool 25 is the maximum

Frame size grows with the bitpool. Measured with `libsbc` for dual channel, 16 blocks, 8 subbands:

| Bitpool | Frame size | 4 frames |
| --- | --- | --- |
| 20 | 92 B | 368 B |
| 21 | 96 B | 384 B |
| 22 | 100 B | 400 B |
| 23 | 104 B | 416 B |
| 24 | 108 B | 432 B |
| **25** | **112 B** | **448 B** (fits exactly) |
| 26 | 116 B | 464 B (16 B too large) |
| 27 | 120 B | 480 B |
| 28 | 124 B | 496 B |

The frame area is exactly 448 bytes, so **bitpool 26 and above do not fit**. There is no spare room
to raise the bitrate within this report layout.

### Why 24-bit is not possible

SBC encodes 16-bit PCM. The encoder input is an array of signed 16-bit samples; a 24-bit source would
have its low 8 bits discarded before encoding. "24-bit over Bluetooth" is not a property of this
transport.

### What about 48 kHz?

The sample rate is part of the SBC header, so the codec itself supports 44.1 and 48 kHz. The problem
is the fixed 16 ms report:

| Approach | Audio per 16 ms | Result |
| --- | --- | --- |
| 48 kHz, 4 frames (128 samples each) | 10.67 ms | 5.33 ms of silence per report; not viable as-is |
| 48 kHz, 6 frames | 16 ms | 448 / 6 = 74.7 B per frame; only bitpool 15 fits (72 B, 216 kbit/s) |
| 48 kHz, 4 frames, report every 10.67 ms | 16 ms | 336 kbit/s, but the non-standard cadence is untested |
| 44.1 kHz, 4 frames, report every 11.61 ms | 16 ms | ~309 kbit/s, same caveat |

None of these is a free upgrade:

- At 48 kHz with six frames the bitrate is the same 216 kbit/s, now spread over more samples, so
  quantization noise per sample is worse. It also assumes the pad parses six variable-length frames
  in the 448-byte area, which was not verified.
- At 48 kHz with a shorter report interval the bitrate rises to 336 kbit/s, but the cadence stops
  matching the 16 ms the pad is used to. That increases the outgoing HID traffic by 50 %, may steal
  time from input reports, and could make the pad's buffer underrun.

Neither variant was implemented. The safe, native configuration is 32 kHz / bitpool 25.

## Muting the speaker while the jack keeps playing

### The control bytes are ignored

The protocol has two ways to silence the speaker from the host:

- the audio target byte: `0x02` speaker, `0x24` headset;
- the speaker volume field in state report `0x11` (and a separate headset volume field).

On the unit tested, **neither works**:

- sending target `0x24` with a dummy plug in the jack did not silence the speaker;
- the speaker volume bytes had no audible effect, which is why volume is applied in software before
  encoding.

### The anti-phase trick

The pad's internal speaker plays the **sum** of the two decoded channels, while the headset jack keeps
them apart. If the right channel is inverted, the sum at the speaker is near zero while the jack still
has a signal in each ear. Inverting one ear's polarity for the whole stream is inaudible on
headphones.

In practice the pad does not sum the channels with exactly the same weight, so the inversion is
scaled by a gain `g`:

```
left  =  L
right = -g * L
speaker = L + w * (-g * L)
```

With `w` close to 1, `g = 1 / w` cancels the speaker. On the unit tested the best value was
**`g = 0.96`**, measured at 1, 4 and 6 kHz. The cost in the right ear is
`20 * log10(0.96) = -0.35 dB`, which is negligible.

### Measured residual

With `g = 0.96`, the acoustic output of the speaker relative to the un-cancelled signal:

| Frequency | Residual |
| --- | --- |
| 1 kHz | about -55 dB |
| 2 kHz | about -49 dB |
| 4 kHz | about -47 dB |
| 6 kHz | about -43 dB |

The residual is **frequency dependent**: it is lowest at 1 kHz and rises toward the high end. With
music it is most noticeable on bright, thin sounds (guitars, cymbals), not on bass.

Two effects plausibly contribute, and both are consistent with the numbers:

1. a frequency-dependent gain/phase mismatch between the pad's left and right output stages, which a
   single scalar `g` cannot correct at every frequency;
2. SBC quantization noise: in dual-channel mode each channel is coded independently, so the two
   error signals do not cancel when the speaker sums the channels. Codec noise is concentrated where
   the ear is less sensitive but is not zero.

A frequency-dependent trim, or a coding mode whose errors cancel in the sum, could reduce the
residual further; neither was implemented or measured.

## USB compared with Bluetooth

Over USB the pad exposes a USB Audio Class device and receives **uncompressed PCM**. Its public USB
descriptor lists 32 kHz, 16-bit, stereo for the output endpoint (and 16 kHz mono for the microphone).
That is the same 32 kHz band as the Bluetooth stream, but without SBC:

| | Bluetooth | USB |
| --- | --- | --- |
| Encoding | SBC, lossy | PCM, lossless |
| Bitrate | ~224 kbit/s | ~1 024 kbit/s |
| Bandwidth | 16 kHz | 16 kHz |
| Transport | HID reports every 16 ms | isochronous USB audio |

The audible difference between the two is therefore mostly the **codec and the transport**, not the
sample rate. The jack and its amplifier are the same in both cases.

## Open questions

- Whether the pad accepts six SBC frames per report, or a report interval other than 16 ms.
- Whether the residual can be reduced with a per-frequency correction instead of a single gain.
- Whether joint-stereo coding changes the residual, given that its quantization errors have a
  different relationship between the channels.
- Whether the control bytes (`0x24`, speaker volume) work on other units; the behaviour above is from
  one unit and may be clone-specific.

## Caveats

- Single unit, single host, one room. Treat the dB figures as indicative.
- The unit tested reported USB id `054C:09CC`. Different batches, clones and the DualSense are out of
  scope.
- This page is independent research. It is not affiliated with, endorsed by or sponsored by Sony
  Interactive Entertainment.

## Sources

- SensePost, "Dual-Pod-Shock: emotional abuse of a DualShock": <https://sensepost.com/blog/2020/dual-pod-shock-emotional-abuse-of-a-dualshock>
- PS4 Developer wiki, DS4-BT (DualShock 4 over Bluetooth): <https://www.psdevwiki.com/ps4/DS4-BT>
- snipem/DS4Dongle: <https://github.com/snipem/DS4Dongle>
- DualShock 4 data structures (controllers wiki): <https://controllers.fandom.com/wiki/Sony_DualShock_4/Data_Structures>
- libsbc (SBC codec): <https://git.kernel.org/pub/scm/bluetooth/sbc.git>
