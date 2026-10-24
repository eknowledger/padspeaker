#!/usr/bin/env python3
"""HISTORICAL PROOF OF CONCEPT. Use ``padspeaker tone`` (or ``tools/box_selftest.py --tone``) instead.

This is the first, self-contained script that proved a DualShock 4 style pad plays audio sent over Bluetooth. It is kept
for one reason: it is an independent implementation of the protocol, and ``tests/test_protocol.py`` compares the real
report builders with it byte for byte. It is not maintained, does not restore the lightbar and does not keep the
Bluetooth link out of sniff mode, so longer runs may stop after about 14 seconds.

Original description: play a test tone on a DualShock 4 style pad's built-in speaker over Bluetooth.

Protocol (from the public reverse-engineering work: SensePost dual-pod-shock, psdevwiki DS4-BT,
and the snipem/DS4Dongle sources):
  * audio is 32 kHz stereo signed 16 bit, SBC encoded: dual channel, 16 blocks, 8 subbands, loudness,
    bitpool 25  -> 112-byte frames (header 9c 75 19), each frame = 128 samples per channel (4 ms)
  * 4 frames are packed into one HID output report 0x17 (462 bytes incl. CRC-32), sent every 16 ms
      [0]=0x17 [1]=0x40 (CRC on) [2]=0xA0 (audio on) [3..4]=frame counter LE (+4 per report)
      [5]=0x02 (speaker; 0x24 would be the headset jack) [6..453]=4 SBC frames, rest zero, [458..461]=CRC-32 LE
  * CRC-32 is computed over 0xA2 followed by the report without its last 4 bytes
  * speaker volume is set with an ordinary 0x11 output report (78 bytes): [1]=0xC0 [2]=0x20,
    payload byte 0 = 0x80 (update speaker volume) and payload byte 21 = volume, i.e. report offset 3+21

Usage: ds4_tone.py [--hidraw /dev/hidrawN] [--seconds 6] [--volume 64] [--freq 440]
Only the pad given (or the first Sony pad found) is touched. Nothing is written to any file.
"""
import argparse
import ctypes
import math
import os
import struct
import sys
import time
import zlib

SBC_FREQ_32000 = 0x01
SBC_BLK_16 = 0x03
SBC_SB_8 = 0x01
SBC_MODE_DUAL_CHANNEL = 0x01
SBC_AM_LOUDNESS = 0x00
SBC_LE = 0x00


class Sbc(ctypes.Structure):
    _fields_ = [("flags", ctypes.c_ulong), ("frequency", ctypes.c_uint8), ("blocks", ctypes.c_uint8),
                ("subbands", ctypes.c_uint8), ("mode", ctypes.c_uint8), ("allocation", ctypes.c_uint8),
                ("bitpool", ctypes.c_uint8), ("endian", ctypes.c_uint8), ("priv", ctypes.c_void_p),
                ("priv_alloc_base", ctypes.c_void_p)]


def make_encoder():
    lib = ctypes.CDLL("libsbc.so.1")
    sbc = Sbc()
    lib.sbc_init(ctypes.byref(sbc), 0)
    sbc.frequency = SBC_FREQ_32000
    sbc.blocks = SBC_BLK_16
    sbc.subbands = SBC_SB_8
    sbc.mode = SBC_MODE_DUAL_CHANNEL
    sbc.allocation = SBC_AM_LOUDNESS
    sbc.bitpool = 25
    sbc.endian = SBC_LE

    def encode(pcm: bytes) -> bytes:
        assert len(pcm) == 512
        out = ctypes.create_string_buffer(256)
        written = ctypes.c_size_t(0)
        used = lib.sbc_encode(ctypes.byref(sbc), pcm, len(pcm), out, len(out), ctypes.byref(written))
        if used != len(pcm) or written.value != 112:
            raise RuntimeError("sbc_encode used=%s written=%s" % (used, written.value))
        return out.raw[:112]
    return encode


def with_crc(body: bytes) -> bytes:
    crc = zlib.crc32(b"\xa2" + body) & 0xFFFFFFFF
    return body + struct.pack("<I", crc)


def volume_report(speaker: int) -> bytes:
    pkt = bytearray(74)
    pkt[0] = 0x11
    pkt[1] = 0xC0
    pkt[2] = 0x20
    pkt[3 + 0] = 0x80            # EnableVolumeSpeakerUpdate
    pkt[3 + 21] = speaker & 0xFF  # VolumeSpeaker
    return with_crc(bytes(pkt))


def audio_report(counter: int, frames) -> bytes:
    pkt = bytearray(458)
    pkt[0] = 0x17
    pkt[1] = 0x40
    pkt[2] = 0xA0
    pkt[3] = counter & 0xFF
    pkt[4] = (counter >> 8) & 0xFF
    pkt[5] = 0x02
    for i, f in enumerate(frames):
        pkt[6 + i * 112:6 + (i + 1) * 112] = f
    return with_crc(bytes(pkt))


def find_pad():
    base = "/sys/class/hidraw"
    for n in sorted(os.listdir(base)):
        try:
            uevent = open(os.path.join(base, n, "device", "uevent")).read()
        except OSError:
            continue
        if "HID_ID=0005:0000054C" in uevent:      # Bluetooth, Sony vendor id
            return "/dev/" + n
    return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--hidraw")
    ap.add_argument("--seconds", type=float, default=6.0)
    ap.add_argument("--volume", type=int, default=64)   # 0..79
    ap.add_argument("--freq", type=float, default=440.0)
    ap.add_argument("--amp", type=float, default=0.25)
    a = ap.parse_args()
    dev = a.hidraw or find_pad()
    if not dev:
        sys.exit("no Bluetooth Sony pad found")
    print("pad:", dev)
    encode = make_encoder()
    fd = os.open(dev, os.O_WRONLY)
    try:
        os.write(fd, volume_report(a.volume))
        print("volume set", a.volume)
        counter = 0
        sample_pos = 0
        t0 = time.monotonic()
        n_reports = int(a.seconds / 0.016)
        for r in range(n_reports):
            frames = []
            for _ in range(4):
                pcm = bytearray()
                for k in range(128):
                    v = int(32767 * a.amp * math.sin(2 * math.pi * a.freq * (sample_pos + k) / 32000.0))
                    pcm += struct.pack("<hh", v, v)
                sample_pos += 128
                frames.append(encode(bytes(pcm)))
            os.write(fd, audio_report(counter, frames))
            counter = (counter + 4) & 0xFFFF
            # pace: report r should leave at t0 + r*16 ms (a little ahead of real time is fine)
            delay = t0 + (r + 1) * 0.016 - 0.03 - time.monotonic()
            if delay > 0:
                time.sleep(delay)
        print("sent %d reports in %.2f s" % (n_reports, time.monotonic() - t0))
    finally:
        os.close(fd)


if __name__ == "__main__":
    main()
