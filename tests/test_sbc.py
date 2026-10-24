"""Tests for padspeaker.sbc.

Most tests inject a fake ctypes library so they run anywhere; the tests at the
bottom use the real libsbc and are skipped when it is not installed.
"""
import ctypes
import math
import struct

import pytest

from padspeaker import sbc


class FakeLib:
    """Stands in for libsbc and records how it was driven."""

    def __init__(self, init_result=0, encode_used=512, encode_written=112, header=b"\x9c\x75\x19"):
        self.init_result = init_result
        self.encode_used = encode_used
        self.encode_written = encode_written
        self.header = header
        self.init_calls = 0
        self.finish_calls = 0
        self.encode_calls = 0
        self.struct = None
        self.init_flags = None
        self.snapshot = None
        self.last_pcm = None
        self.last_output_len = None

    def sbc_init(self, ref, flags):
        self.init_calls += 1
        self.struct = ref._obj
        self.init_flags = flags
        return self.init_result

    def sbc_encode(self, ref, pcm, pcm_len, out, out_len, written_ref):
        self.encode_calls += 1
        s = ref._obj
        self.snapshot = dict(
            flags=s.flags, frequency=s.frequency, blocks=s.blocks, subbands=s.subbands,
            mode=s.mode, allocation=s.allocation, bitpool=s.bitpool, endian=s.endian)
        self.last_pcm = bytes(pcm)
        self.last_output_len = out_len
        data = (self.header + bytes(range(109)))[:112]
        ctypes.memmove(out, data, len(data))
        written_ref._obj.value = self.encode_written
        return self.encode_used

    def sbc_finish(self, ref):
        self.finish_calls += 1


PCM = b"\x00" * 512


# --- constants and struct layout ---------------------------------------------

def test_constants_match_libsbc_header():
    assert sbc.SBC_FREQ_32000 == 0x01
    assert sbc.SBC_BLK_16 == 0x03
    assert sbc.SBC_SB_8 == 0x01
    assert sbc.SBC_MODE_DUAL_CHANNEL == 0x01
    assert sbc.SBC_AM_LOUDNESS == 0x00
    assert sbc.SBC_LE == 0x00


def test_struct_layout_matches_sbc_h():
    names = [name for name, _ in sbc.SbcStruct._fields_]
    assert names == ["flags", "frequency", "blocks", "subbands", "mode", "allocation",
                     "bitpool", "endian", "priv", "priv_alloc_base"]
    types = dict(sbc.SbcStruct._fields_)
    assert types["flags"] is ctypes.c_ulong
    for name in ("frequency", "blocks", "subbands", "mode", "allocation", "bitpool", "endian"):
        assert types[name] is ctypes.c_uint8
    assert types["priv"] is ctypes.c_void_p
    assert types["priv_alloc_base"] is ctypes.c_void_p
    # unsigned long, 7 bytes, padding to pointer alignment, two pointers
    ulong = ctypes.sizeof(ctypes.c_ulong)
    ptr = ctypes.sizeof(ctypes.c_void_p)
    offset = ulong + 7
    offset += (-offset) % ptr
    assert ctypes.sizeof(sbc.SbcStruct) == offset + 2 * ptr


# --- configuration -----------------------------------------------------------

def test_encoder_initialises_and_configures_fixed_settings():
    lib = FakeLib()
    with sbc.SbcEncoder(lib=lib) as enc:
        assert lib.init_calls == 1
        assert lib.init_flags == 0
        enc.encode(PCM)
    assert lib.snapshot == dict(flags=0, frequency=0x01, blocks=0x03, subbands=0x01,
                                mode=0x01, allocation=0x00, bitpool=25, endian=0x00)


def test_encode_returns_exactly_one_frame_with_header():
    lib = FakeLib()
    with sbc.SbcEncoder(lib=lib) as enc:
        frame = enc.encode(PCM)
    assert isinstance(frame, bytes)
    assert len(frame) == 112
    assert frame[:3] == b"\x9c\x75\x19"


def test_encode_passes_pcm_and_offers_big_enough_output():
    lib = FakeLib()
    pcm = bytes(range(256)) * 2
    with sbc.SbcEncoder(lib=lib) as enc:
        enc.encode(pcm)
    assert lib.last_pcm == pcm
    assert lib.last_output_len >= 112


def test_encode_accepts_bytearray_and_memoryview():
    lib = FakeLib()
    with sbc.SbcEncoder(lib=lib) as enc:
        assert len(enc.encode(bytearray(512))) == 112
        assert len(enc.encode(memoryview(bytes(512)))) == 112


@pytest.mark.parametrize("size", [0, 1, 511, 513, 1024, 2048])
def test_encode_rejects_wrong_input_size(size):
    lib = FakeLib()
    with sbc.SbcEncoder(lib=lib) as enc:
        with pytest.raises(sbc.SbcError, match="512"):
            enc.encode(b"\x00" * size)
    assert lib.encode_calls == 0


def test_encode_rejects_non_bytes():
    with sbc.SbcEncoder(lib=FakeLib()) as enc:
        with pytest.raises(sbc.SbcError):
            enc.encode("x" * 512)  # type: ignore[arg-type]


def test_encode_error_from_library_is_reported():
    lib = FakeLib(encode_used=-22)
    with sbc.SbcEncoder(lib=lib) as enc:
        with pytest.raises(sbc.SbcError, match="-22"):
            enc.encode(PCM)


def test_encode_partial_consumption_is_an_error():
    lib = FakeLib(encode_used=256)
    with sbc.SbcEncoder(lib=lib) as enc:
        with pytest.raises(sbc.SbcError, match="256"):
            enc.encode(PCM)


@pytest.mark.parametrize("written", [0, 111, 113])
def test_encode_wrong_output_size_is_an_error(written):
    lib = FakeLib(encode_written=written)
    with sbc.SbcEncoder(lib=lib) as enc:
        with pytest.raises(sbc.SbcError, match="112"):
            enc.encode(PCM)


def test_encode_wrong_header_is_an_error():
    lib = FakeLib(header=b"\xde\xad\xbe")
    with sbc.SbcEncoder(lib=lib) as enc:
        with pytest.raises(sbc.SbcError, match="header"):
            enc.encode(PCM)


def test_init_failure_raises_and_does_not_leak():
    lib = FakeLib(init_result=-12)
    with pytest.raises(sbc.SbcError, match="-12"):
        sbc.SbcEncoder(lib=lib)


# --- lifecycle ---------------------------------------------------------------

def test_close_frees_once_and_is_idempotent():
    lib = FakeLib()
    enc = sbc.SbcEncoder(lib=lib)
    enc.close()
    enc.close()
    assert lib.finish_calls == 1


def test_context_manager_frees_on_exit_and_on_error():
    lib = FakeLib()
    with pytest.raises(RuntimeError):
        with sbc.SbcEncoder(lib=lib):
            raise RuntimeError("boom")
    assert lib.finish_calls == 1


def test_encode_after_close_raises():
    enc = sbc.SbcEncoder(lib=FakeLib())
    enc.close()
    with pytest.raises(sbc.SbcError, match="closed"):
        enc.encode(PCM)


def test_reset_reinitialises_the_encoder():
    lib = FakeLib()
    with sbc.SbcEncoder(lib=lib) as enc:
        enc.reset()
        assert lib.finish_calls == 1
        assert lib.init_calls == 2
        enc.encode(PCM)
    assert lib.snapshot["bitpool"] == 25
    assert lib.finish_calls == 2


# --- library discovery -------------------------------------------------------

def test_missing_library_raises_unavailable_with_install_hint(monkeypatch):
    monkeypatch.setattr(sbc.ctypes.util, "find_library", lambda name: None)

    def refuse(name):
        raise OSError("cannot open %s" % name)

    monkeypatch.setattr(sbc.ctypes, "CDLL", refuse)
    with pytest.raises(sbc.SbcUnavailable) as info:
        sbc.SbcEncoder()
    message = str(info.value)
    assert "libsbc" in message
    assert "apt" in message and "brew" in message
    assert isinstance(info.value, sbc.SbcError)
    assert sbc.available() is False


def test_library_lookup_order_is_find_library_then_soname(monkeypatch):
    tried = []
    monkeypatch.setattr(sbc.ctypes.util, "find_library", lambda name: "libfound.so")

    def fake_cdll(name):
        tried.append(name)
        raise OSError(name)

    monkeypatch.setattr(sbc.ctypes, "CDLL", fake_cdll)
    assert sbc.available() is False
    assert tried == ["libfound.so", "libsbc.so.1"]


def test_available_true_when_library_loads(monkeypatch):
    monkeypatch.setattr(sbc.ctypes.util, "find_library", lambda name: None)
    monkeypatch.setattr(sbc.ctypes, "CDLL", lambda name: FakeLib())
    assert sbc.available() is True


# --- real libsbc -------------------------------------------------------------

needs_libsbc = pytest.mark.skipif(not sbc.available(), reason="libsbc is not installed")


def _sine(frames, freq=440.0, offset=0):
    out = bytearray()
    for i in range(frames):
        v = int(8000 * math.sin(2 * math.pi * freq * (offset + i) / 32000.0))
        out += struct.pack("<hh", v, v)
    return bytes(out)


@needs_libsbc
def test_real_encoder_frame_size_and_header():
    with sbc.SbcEncoder() as enc:
        frame = enc.encode(_sine(128))
    assert len(frame) == 112
    assert frame[:3] == b"\x9c\x75\x19"


@needs_libsbc
def test_real_encoder_streams_many_frames():
    with sbc.SbcEncoder() as enc:
        frames = [enc.encode(_sine(128, offset=128 * n)) for n in range(20)]
    assert all(len(f) == 112 and f[:3] == b"\x9c\x75\x19" for f in frames)
    assert len(set(frames)) > 1  # real audio, not a constant


@needs_libsbc
def test_real_encoder_silence_still_yields_full_frames():
    with sbc.SbcEncoder() as enc:
        assert len(enc.encode(PCM)) == 112
