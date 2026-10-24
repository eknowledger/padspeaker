"""SBC encoding through libsbc (ctypes), with the DualShock 4 stream settings.

The pad's speaker expects SBC frames with fixed parameters: 32 kHz, 16 blocks,
8 subbands, dual channel, loudness allocation, bitpool 25, little endian. With
those settings one frame is 112 bytes (header ``9c 75 19``) and encodes 128
stereo samples, i.e. 512 bytes of signed 16 bit little endian PCM (4 ms).

``ffmpeg`` cannot be asked for "dual channel", which is why this module talks
to libsbc directly. Only the standard library is used; the shared library is
located at runtime.

Example::

    if sbc.available():
        with sbc.SbcEncoder() as encoder:
            frame = encoder.encode(pcm_512_bytes)   # -> 112 bytes
"""
import ctypes
import ctypes.util
from typing import Any, Optional

__all__ = [
    "SBC_FREQ_32000",
    "SBC_BLK_16",
    "SBC_SB_8",
    "SBC_MODE_DUAL_CHANNEL",
    "SBC_AM_LOUDNESS",
    "SBC_LE",
    "BITPOOL",
    "PCM_BYTES",
    "FRAME_BYTES",
    "FRAME_HEADER",
    "SbcError",
    "SbcUnavailable",
    "SbcStruct",
    "SbcEncoder",
    "available",
]

# Constants from libsbc's sbc.h.
SBC_FREQ_32000 = 0x01
SBC_BLK_16 = 0x03
SBC_SB_8 = 0x01
SBC_MODE_DUAL_CHANNEL = 0x01
SBC_AM_LOUDNESS = 0x00
SBC_LE = 0x00

BITPOOL = 25
"""Bitpool giving 112-byte frames with the settings above."""

PCM_BYTES = 512
"""Input size of one encode call: 128 samples x 2 channels x 2 bytes."""

FRAME_BYTES = 112
"""Output size of one encode call."""

FRAME_HEADER = b"\x9c\x75\x19"
"""Expected first three bytes of every frame (syncword, config, bitpool)."""

_SONAME = "libsbc.so.1"
_OUTPUT_BUFFER_BYTES = 256  # comfortably larger than one frame


class SbcError(Exception):
    """An SBC encoding step failed."""


class SbcUnavailable(SbcError):
    """libsbc could not be loaded; the message explains how to install it."""


class SbcStruct(ctypes.Structure):
    """Mirror of ``sbc_t`` (``struct sbc_struct``) from libsbc's ``sbc.h``."""

    _fields_ = [
        ("flags", ctypes.c_ulong),
        ("frequency", ctypes.c_uint8),
        ("blocks", ctypes.c_uint8),
        ("subbands", ctypes.c_uint8),
        ("mode", ctypes.c_uint8),
        ("allocation", ctypes.c_uint8),
        ("bitpool", ctypes.c_uint8),
        ("endian", ctypes.c_uint8),
        ("priv", ctypes.c_void_p),
        ("priv_alloc_base", ctypes.c_void_p),
    ]


def _load_library() -> Any:
    """Load libsbc, trying ``find_library('sbc')`` and then ``libsbc.so.1``.

    Raises:
        SbcUnavailable: if neither name can be loaded.
    """
    candidates = []
    found = ctypes.util.find_library("sbc")
    if found:
        candidates.append(found)
    if _SONAME not in candidates:
        candidates.append(_SONAME)
    errors = []
    for name in candidates:
        try:
            return ctypes.CDLL(name)
        except OSError as exc:
            errors.append("%s: %s" % (name, exc))
    raise SbcUnavailable(
        "libsbc could not be loaded (%s). Install it, for example: "
        "'apt install libsbc1' (Debian, Ubuntu), 'dnf install sbc' (Fedora), "
        "'pacman -S sbc' (Arch) or 'brew install sbc' (macOS)."
        % ("; ".join(errors) or "no candidate names")
    )


def _declare_prototypes(lib: Any) -> None:
    """Give the three libsbc functions we use explicit C prototypes."""
    lib.sbc_init.argtypes = [ctypes.c_void_p, ctypes.c_ulong]
    lib.sbc_init.restype = ctypes.c_int
    lib.sbc_encode.argtypes = [
        ctypes.c_void_p,                  # sbc_t *
        ctypes.c_void_p,                  # const void *input
        ctypes.c_size_t,                  # input_len
        ctypes.c_void_p,                  # void *output
        ctypes.c_size_t,                  # output_len
        ctypes.POINTER(ctypes.c_ssize_t),  # ssize_t *written
    ]
    lib.sbc_encode.restype = ctypes.c_ssize_t
    lib.sbc_finish.argtypes = [ctypes.c_void_p]
    lib.sbc_finish.restype = None


def available() -> bool:
    """Return True if libsbc can be loaded on this machine."""
    try:
        _load_library()
    except SbcUnavailable:
        return False
    return True


class SbcEncoder:
    """Stateful SBC encoder producing DualShock 4 speaker frames.

    SBC keeps filter-bank history between frames, so use one encoder per audio
    stream and feed it consecutive 512-byte chunks. Call :meth:`reset` (or make
    a new encoder) when a stream restarts after a gap.

    Args:
        lib: an already loaded libsbc-like object. Meant for tests; by default
            the real library is located and loaded.

    Raises:
        SbcUnavailable: if libsbc is missing.
        SbcError: if libsbc fails to initialise.
    """

    def __init__(self, lib: Optional[Any] = None) -> None:
        if lib is None:
            lib = _load_library()
            _declare_prototypes(lib)
        self._lib = lib
        self._sbc: Optional[SbcStruct] = None
        self._out = ctypes.create_string_buffer(_OUTPUT_BUFFER_BYTES)
        self._open()

    def _open(self) -> None:
        state = SbcStruct()
        result = self._lib.sbc_init(ctypes.byref(state), 0)
        if result < 0:
            raise SbcError("sbc_init failed with code %d" % result)
        state.frequency = SBC_FREQ_32000
        state.blocks = SBC_BLK_16
        state.subbands = SBC_SB_8
        state.mode = SBC_MODE_DUAL_CHANNEL
        state.allocation = SBC_AM_LOUDNESS
        state.bitpool = BITPOOL
        state.endian = SBC_LE
        self._sbc = state

    def encode(self, pcm: bytes) -> bytes:
        """Encode exactly 512 bytes of s16le stereo PCM into one 112-byte frame.

        Raises:
            SbcError: if the encoder is closed, the input is not exactly 512
                bytes, or libsbc returns anything other than one full frame
                starting with ``9c 75 19``.
        """
        if self._sbc is None:
            raise SbcError("encoder is closed")
        if not isinstance(pcm, (bytes, bytearray, memoryview)):
            raise SbcError("pcm must be bytes-like, got %s" % type(pcm).__name__)
        data = bytes(pcm)
        if len(data) != PCM_BYTES:
            raise SbcError("encode() needs exactly %d bytes of PCM (128 stereo samples), got %d"
                           % (PCM_BYTES, len(data)))
        written = ctypes.c_ssize_t(0)
        used = self._lib.sbc_encode(
            ctypes.byref(self._sbc), data, len(data),
            self._out, len(self._out), ctypes.byref(written))
        if used < 0:
            raise SbcError("sbc_encode failed with code %d" % used)
        if used != PCM_BYTES:
            raise SbcError("sbc_encode consumed %d of %d bytes" % (used, PCM_BYTES))
        if written.value != FRAME_BYTES:
            raise SbcError("sbc_encode produced %d bytes, expected %d"
                           % (written.value, FRAME_BYTES))
        frame = self._out.raw[:FRAME_BYTES]
        if frame[:3] != FRAME_HEADER:
            raise SbcError("unexpected SBC frame header %s, expected %s"
                           % (frame[:3].hex(), FRAME_HEADER.hex()))
        return frame

    def reset(self) -> None:
        """Discard the filter-bank history by re-initialising the encoder."""
        self.close()
        self._open()

    def close(self) -> None:
        """Release libsbc state. Safe to call more than once."""
        state, self._sbc = self._sbc, None
        if state is not None:
            self._lib.sbc_finish(ctypes.byref(state))

    def __enter__(self) -> "SbcEncoder":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def __del__(self) -> None:
        try:
            self.close()
        except Exception:
            pass
