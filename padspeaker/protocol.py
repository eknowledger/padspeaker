"""DualShock 4 Bluetooth speaker protocol: report builders and constants.

This module is pure: it performs no I/O and has no dependencies beyond the
standard library. It knows how to lay out the two kinds of HID output report
the library ever writes:

* Output report ``0x17`` (462 bytes): four SBC audio frames, sent every 16 ms.
* Output report ``0x11`` (78 bytes): controller state, used here for the
  speaker volume and the lightbar colour.

Every report ends with a CRC-32 computed over a fixed ``0xA2`` prefix byte
followed by the report body. Reports are written to the pad's ``/dev/hidrawN``
node with a single ``write()`` of the complete report, report id included.

The layout follows the public reverse-engineering work (SensePost
dual-pod-shock, psdevwiki DS4-BT, snipem/DS4Dongle) and was verified on real
hardware.
"""
import struct
import zlib
from typing import NamedTuple, Optional, Sequence, Tuple

__all__ = [
    "SAMPLE_RATE",
    "SBC_FRAME_BYTES",
    "SAMPLES_PER_FRAME",
    "FRAMES_PER_REPORT",
    "REPORT_SECONDS",
    "PCM_BYTES_PER_FRAME",
    "PCM_BYTES_PER_REPORT",
    "AUDIO_REPORT_ID",
    "AUDIO_REPORT_LEN",
    "STATE_REPORT_ID",
    "STATE_REPORT_LEN",
    "TARGET_SPEAKER",
    "TARGET_HEADSET",
    "MAX_SPEAKER_VOLUME",
    "AudioReport",
    "FrameCounter",
    "crc32_report",
    "build_audio_report",
    "build_volume_report",
    "build_lightbar_report",
    "build_state_report",
    "is_audio_report",
    "is_state_report",
    "parse_audio_report",
]

# --- stream parameters -------------------------------------------------------

SAMPLE_RATE = 32000
"""PCM sample rate in Hz (stereo, signed 16 bit little endian)."""

SBC_FRAME_BYTES = 112
"""Size of one SBC frame: dual channel, 16 blocks, 8 subbands, bitpool 25."""

SAMPLES_PER_FRAME = 128
"""PCM samples per channel carried by one SBC frame (4 ms at 32 kHz)."""

FRAMES_PER_REPORT = 4
"""SBC frames packed into one audio report."""

REPORT_SECONDS = 0.016
"""Interval between audio reports (``FRAMES_PER_REPORT`` frames of 4 ms)."""

PCM_BYTES_PER_FRAME = SAMPLES_PER_FRAME * 2 * 2
"""Bytes of s16le stereo PCM consumed by one SBC frame (512)."""

PCM_BYTES_PER_REPORT = PCM_BYTES_PER_FRAME * FRAMES_PER_REPORT
"""Bytes of s16le stereo PCM consumed by one audio report (2048)."""

# --- report framing ----------------------------------------------------------

AUDIO_REPORT_ID = 0x17
AUDIO_REPORT_LEN = 462
STATE_REPORT_ID = 0x11
STATE_REPORT_LEN = 78

TARGET_SPEAKER = 0x02
"""Audio target byte: the built-in speaker."""

TARGET_HEADSET = 0x24
"""Audio target byte: the headset jack."""

MAX_SPEAKER_VOLUME = 0x4F
"""Largest speaker volume the pad accepts."""

_CRC_PREFIX = b"\xa2"
_CRC_BYTES = 4
_COUNTER_MODULUS = 0x10000
_COUNTER_STEP = FRAMES_PER_REPORT

_AUDIO_HEADER = bytes([AUDIO_REPORT_ID, 0x40, 0xA0])  # id, CRC enabled, audio enabled
_AUDIO_BODY_LEN = AUDIO_REPORT_LEN - _CRC_BYTES  # 458, includes the zero padding
_AUDIO_FRAMES_OFFSET = 6
_AUDIO_COUNTER_OFFSET = 3
_AUDIO_TARGET_OFFSET = 5

_STATE_HEADER = bytes([STATE_REPORT_ID, 0xC0, 0x20])  # id, CRC enabled, state update
_STATE_BODY_LEN = STATE_REPORT_LEN - _CRC_BYTES  # 74
_STATE_PAYLOAD = 3  # offset of the 71-byte state block
_FLAG_LIGHTBAR = 0x02
_FLAG_SPEAKER_VOLUME = 0x80
_LIGHTBAR_OFFSET = _STATE_PAYLOAD + 5
_SPEAKER_VOLUME_OFFSET = _STATE_PAYLOAD + 21

_VALID_TARGETS = (TARGET_SPEAKER, TARGET_HEADSET)


def crc32_report(body: bytes) -> bytes:
    """Return ``body`` with the report CRC-32 appended (little endian).

    The checksum is the standard CRC-32 (as in :func:`zlib.crc32`) computed
    over the byte ``0xA2`` followed by ``body``. The ``0xA2`` prefix is not
    part of the returned bytes.

    Raises:
        TypeError: if ``body`` is not bytes-like.
    """
    if not isinstance(body, (bytes, bytearray, memoryview)):
        raise TypeError("report body must be bytes-like, got %s" % type(body).__name__)
    crc = zlib.crc32(_CRC_PREFIX + bytes(body)) & 0xFFFFFFFF
    return bytes(body) + struct.pack("<I", crc)


class FrameCounter:
    """Frame counter carried in each audio report.

    The counter advances by ``FRAMES_PER_REPORT`` (4) per report and wraps at
    16 bits. :meth:`next` returns the value to use for the report about to be
    built and then advances.
    """

    def __init__(self, start: int = 0) -> None:
        self._value = 0
        self.reset(start)

    @property
    def value(self) -> int:
        """The counter value the next report will carry."""
        return self._value

    def reset(self, start: int = 0) -> None:
        """Restart the counter at ``start`` (default 0)."""
        if not 0 <= start < _COUNTER_MODULUS:
            raise ValueError("counter must be in 0..0xFFFF, got %r" % (start,))
        self._value = start

    def next(self) -> int:
        """Return the current value and advance by 4, wrapping at 16 bits."""
        current = self._value
        self._value = (current + _COUNTER_STEP) % _COUNTER_MODULUS
        return current


def build_audio_report(
    counter: int,
    frames: Sequence[bytes],
    target: int = TARGET_SPEAKER,
) -> bytes:
    """Build one 462-byte audio output report (id ``0x17``).

    Layout: ``[0]=0x17 [1]=0x40 [2]=0xA0 [3:5]=counter (LE) [5]=target
    [6:454]=4 SBC frames [454:458]=zero [458:462]=CRC-32``.

    Args:
        counter: 16-bit frame counter, ``0..0xFFFF`` (see :class:`FrameCounter`).
        frames: exactly ``FRAMES_PER_REPORT`` SBC frames of ``SBC_FRAME_BYTES`` each.
        target: ``TARGET_SPEAKER`` (default) or ``TARGET_HEADSET``.

    Raises:
        ValueError: if the counter, target, frame count or frame size is invalid.
    """
    if not 0 <= counter < _COUNTER_MODULUS:
        raise ValueError("counter must be in 0..0xFFFF, got %r" % (counter,))
    if target not in _VALID_TARGETS:
        raise ValueError("unknown audio target 0x%02X" % (target,))
    if len(frames) != FRAMES_PER_REPORT:
        raise ValueError("an audio report needs exactly %d frames, got %d"
                         % (FRAMES_PER_REPORT, len(frames)))
    for index, frame in enumerate(frames):
        if len(frame) != SBC_FRAME_BYTES:
            raise ValueError("frame %d must be exactly %d bytes, got %d"
                             % (index, SBC_FRAME_BYTES, len(frame)))

    body = bytearray(_AUDIO_BODY_LEN)
    body[0:3] = _AUDIO_HEADER
    body[_AUDIO_COUNTER_OFFSET:_AUDIO_COUNTER_OFFSET + 2] = struct.pack("<H", counter)
    body[_AUDIO_TARGET_OFFSET] = target
    for index, frame in enumerate(frames):
        start = _AUDIO_FRAMES_OFFSET + index * SBC_FRAME_BYTES
        body[start:start + SBC_FRAME_BYTES] = frame
    report = crc32_report(bytes(body))
    assert len(report) == AUDIO_REPORT_LEN
    return report


def build_state_report(
    volume: Optional[int] = None,
    rgb: Optional[Tuple[int, int, int]] = None,
) -> bytes:
    """Build one 78-byte state output report (id ``0x11``).

    Sets the speaker volume, the lightbar colour, or both in a single report;
    only the features that are given are flagged for update.

    Args:
        volume: speaker volume; clamped to ``0..MAX_SPEAKER_VOLUME``.
        rgb: lightbar colour as ``(red, green, blue)``, each ``0..255``.

    Raises:
        ValueError: if neither feature is given or a colour channel is out of range.
    """
    if volume is None and rgb is None:
        raise ValueError("a state report must update the volume, the lightbar, or both")
    body = bytearray(_STATE_BODY_LEN)
    body[0:3] = _STATE_HEADER
    flags = 0
    if rgb is not None:
        if len(rgb) != 3 or any(not 0 <= c <= 255 for c in rgb):
            raise ValueError("lightbar colour must be three values in 0..255, got %r" % (rgb,))
        flags |= _FLAG_LIGHTBAR
        body[_LIGHTBAR_OFFSET:_LIGHTBAR_OFFSET + 3] = bytes(rgb)
    if volume is not None:
        flags |= _FLAG_SPEAKER_VOLUME
        body[_SPEAKER_VOLUME_OFFSET] = max(0, min(int(volume), MAX_SPEAKER_VOLUME))
    body[_STATE_PAYLOAD] = flags
    report = crc32_report(bytes(body))
    assert len(report) == STATE_REPORT_LEN
    return report


def build_volume_report(speaker: int) -> bytes:
    """Build a state report that sets only the speaker volume.

    ``speaker`` is clamped to ``0..MAX_SPEAKER_VOLUME`` (0x4F).
    """
    return build_state_report(volume=speaker)


def build_lightbar_report(red: int, green: int, blue: int) -> bytes:
    """Build a state report that sets only the lightbar colour (each ``0..255``)."""
    return build_state_report(rgb=(red, green, blue))


def is_audio_report(report: bytes) -> bool:
    """Return True if ``report`` has the id and length of an audio report."""
    return len(report) == AUDIO_REPORT_LEN and report[0] == AUDIO_REPORT_ID


def is_state_report(report: bytes) -> bool:
    """Return True if ``report`` has the id and length of a state report."""
    return len(report) == STATE_REPORT_LEN and report[0] == STATE_REPORT_ID


class AudioReport(NamedTuple):
    """Decoded fields of an audio report, as returned by :func:`parse_audio_report`."""

    counter: int
    target: int
    frames: Tuple[bytes, ...]
    crc_ok: bool


def parse_audio_report(report: bytes) -> AudioReport:
    """Decode an audio report (mainly for tests and diagnostics).

    Raises:
        ValueError: if ``report`` is not an audio report of the right length.
    """
    if not is_audio_report(report):
        raise ValueError("not a %d-byte audio report (id 0x%02X)"
                         % (AUDIO_REPORT_LEN, AUDIO_REPORT_ID))
    report = bytes(report)
    counter = struct.unpack_from("<H", report, _AUDIO_COUNTER_OFFSET)[0]
    frames = tuple(
        report[_AUDIO_FRAMES_OFFSET + i * SBC_FRAME_BYTES:
               _AUDIO_FRAMES_OFFSET + (i + 1) * SBC_FRAME_BYTES]
        for i in range(FRAMES_PER_REPORT)
    )
    crc_ok = crc32_report(report[:_AUDIO_BODY_LEN]) == report
    return AudioReport(counter, report[_AUDIO_TARGET_OFFSET], frames, crc_ok)
