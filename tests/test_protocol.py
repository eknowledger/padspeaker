"""Tests for padspeaker.protocol: byte layout, CRC and counters."""
import importlib.util
import os
import struct
import zlib

import pytest

from padspeaker import protocol as p

POC_PATH = os.path.join(os.path.dirname(__file__), "..", "tools", "ds4_tone_poc.py")


def _frames(fill=None):
    """Four distinguishable 112-byte frames."""
    if fill is None:
        fill = [0x11, 0x22, 0x33, 0x44]
    return [bytes([f]) * 112 for f in fill]


def _load_poc():
    spec = importlib.util.spec_from_file_location("ds4_tone_poc", POC_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# --- constants ---------------------------------------------------------------

def test_constants_match_design():
    assert p.SAMPLE_RATE == 32000
    assert p.SBC_FRAME_BYTES == 112
    assert p.SAMPLES_PER_FRAME == 128
    assert p.FRAMES_PER_REPORT == 4
    assert p.REPORT_SECONDS == 0.016
    assert p.AUDIO_REPORT_ID == 0x17
    assert p.AUDIO_REPORT_LEN == 462
    assert p.STATE_REPORT_ID == 0x11
    assert p.STATE_REPORT_LEN == 78
    assert p.TARGET_SPEAKER == 0x02
    assert p.TARGET_HEADSET == 0x24
    assert p.MAX_SPEAKER_VOLUME == 0x4F


def test_timing_is_consistent():
    # 128 samples at 32 kHz is 4 ms; four of them are one 16 ms report.
    assert p.SAMPLES_PER_FRAME / p.SAMPLE_RATE * p.FRAMES_PER_REPORT == pytest.approx(
        p.REPORT_SECONDS)
    assert p.PCM_BYTES_PER_FRAME == 512
    assert p.PCM_BYTES_PER_REPORT == 2048
    assert 6 + 4 * 112 + 4 + 4 == p.AUDIO_REPORT_LEN


# --- CRC ---------------------------------------------------------------------

def test_crc32_report_appends_little_endian_crc_over_a2_prefix():
    body = bytes(range(10))
    out = p.crc32_report(body)
    assert out[:-4] == body
    expected = zlib.crc32(b"\xa2" + body) & 0xFFFFFFFF
    assert out[-4:] == struct.pack("<I", expected)


def test_crc32_report_hand_computed_vector():
    # Independently derived: crc32(0xA2 || 0x11 0xC0 0x20) with zlib, written out.
    body = bytes([0x11, 0xC0, 0x20])
    crc = zlib.crc32(bytes([0xA2, 0x11, 0xC0, 0x20]))
    expected = body + crc.to_bytes(4, "little")
    assert p.crc32_report(body) == expected
    # The prefix must matter: without 0xA2 the CRC is different.
    assert zlib.crc32(body) != crc


def test_crc32_report_accepts_bytearray_and_rejects_non_bytes():
    assert p.crc32_report(bytearray(b"abc")) == p.crc32_report(b"abc")
    with pytest.raises(TypeError):
        p.crc32_report("abc")  # type: ignore[arg-type]


# --- audio report ------------------------------------------------------------

def test_audio_report_length_and_fixed_header():
    r = p.build_audio_report(0, _frames())
    assert isinstance(r, bytes)
    assert len(r) == 462
    assert r[0] == 0x17
    assert r[1] == 0x40
    assert r[2] == 0xA0
    assert r[5] == 0x02


def test_audio_report_layout_offsets():
    frames = _frames()
    r = p.build_audio_report(0x1234, frames)
    assert r[3] == 0x34 and r[4] == 0x12  # little endian counter
    for i, f in enumerate(frames):
        start = 6 + 112 * i
        assert r[start:start + 112] == f
    assert r[454:458] == b"\x00\x00\x00\x00"
    crc = zlib.crc32(b"\xa2" + r[:458]) & 0xFFFFFFFF
    assert r[458:462] == struct.pack("<I", crc)


def test_audio_report_headset_target():
    r = p.build_audio_report(0, _frames(), target=p.TARGET_HEADSET)
    assert r[5] == 0x24
    assert len(r) == 462


def test_audio_report_rejects_unknown_target():
    with pytest.raises(ValueError):
        p.build_audio_report(0, _frames(), target=0x55)


@pytest.mark.parametrize("count", [0, 1, 3, 5])
def test_audio_report_requires_exactly_four_frames(count):
    with pytest.raises(ValueError, match="4 frames"):
        p.build_audio_report(0, _frames([1, 2, 3, 4, 5])[:count])


@pytest.mark.parametrize("size", [0, 111, 113, 512])
def test_audio_report_requires_112_byte_frames(size):
    frames = _frames()
    frames[2] = b"\x00" * size
    with pytest.raises(ValueError, match="112"):
        p.build_audio_report(0, frames)


@pytest.mark.parametrize("counter", [-1, 0x10000, 1 << 20])
def test_audio_report_rejects_counter_out_of_range(counter):
    with pytest.raises(ValueError, match="counter"):
        p.build_audio_report(counter, _frames())


def test_audio_report_accepts_counter_bounds():
    assert len(p.build_audio_report(0, _frames())) == 462
    r = p.build_audio_report(0xFFFF, _frames())
    assert r[3] == 0xFF and r[4] == 0xFF


def test_audio_report_accepts_bytearray_frames():
    r = p.build_audio_report(0, [bytearray(112)] * 4)
    assert len(r) == 462


def test_audio_report_matches_poc_bytes_exactly():
    poc = _load_poc()
    for counter in (0, 4, 0x00FF, 0x1234, 0xFFFC):
        frames = _frames([counter & 0xFF, 0xA5, 0x5A, (counter >> 8) & 0xFF])
        assert p.build_audio_report(counter, frames) == poc.audio_report(counter, frames)


# --- frame counter -----------------------------------------------------------

def test_frame_counter_steps_by_four_and_wraps_at_16_bits():
    c = p.FrameCounter()
    assert c.value == 0
    assert c.next() == 0
    assert c.next() == 4
    assert c.value == 8
    c = p.FrameCounter(0xFFFC)
    assert c.next() == 0xFFFC
    assert c.next() == 0  # wrapped
    c = p.FrameCounter(0xFFFE)
    assert c.next() == 0xFFFE
    assert c.value == 2


def test_frame_counter_validates_start_and_reset():
    with pytest.raises(ValueError):
        p.FrameCounter(0x10000)
    c = p.FrameCounter()
    c.next()
    c.reset()
    assert c.value == 0
    c.reset(100)
    assert c.value == 100


def test_frame_counter_output_is_always_buildable():
    c = p.FrameCounter(0xFFF0)
    for _ in range(10):
        assert len(p.build_audio_report(c.next(), _frames())) == 462


# --- state report (volume, lightbar) -----------------------------------------

def test_volume_report_layout():
    r = p.build_volume_report(0x40)
    assert len(r) == 78
    assert r[0] == 0x11 and r[1] == 0xC0 and r[2] == 0x20
    assert r[3] == 0x80
    assert r[3 + 21] == 0x40
    assert r[3 + 5:3 + 8] == b"\x00\x00\x00"
    crc = zlib.crc32(b"\xa2" + r[:74]) & 0xFFFFFFFF
    assert r[74:78] == struct.pack("<I", crc)


@pytest.mark.parametrize("given,expected", [(-5, 0), (0, 0), (0x4F, 0x4F), (0x50, 0x4F), (999, 0x4F)])
def test_volume_report_clamps(given, expected):
    r = p.build_volume_report(given)
    assert len(r) == 78
    assert r[3 + 21] == expected


def test_volume_report_matches_poc_bytes():
    poc = _load_poc()
    for v in (0, 1, 64, 0x4F):
        assert p.build_volume_report(v) == poc.volume_report(v)


def test_lightbar_report_layout():
    r = p.build_lightbar_report(1, 2, 3)
    assert len(r) == 78
    assert r[:3] == bytes([0x11, 0xC0, 0x20])
    assert r[3] == 0x02
    assert r[3 + 5:3 + 8] == bytes([1, 2, 3])
    assert r[3 + 21] == 0
    crc = zlib.crc32(b"\xa2" + r[:74]) & 0xFFFFFFFF
    assert r[74:78] == struct.pack("<I", crc)


@pytest.mark.parametrize("rgb", [(-1, 0, 0), (0, 256, 0), (0, 0, 1000)])
def test_lightbar_report_rejects_out_of_range(rgb):
    with pytest.raises(ValueError):
        p.build_lightbar_report(*rgb)


def test_state_report_combines_flags():
    r = p.build_state_report(volume=0x20, rgb=(10, 20, 30))
    assert len(r) == 78
    assert r[3] == 0x82
    assert r[3 + 21] == 0x20
    assert r[3 + 5:3 + 8] == bytes([10, 20, 30])


def test_state_report_single_features_equal_dedicated_builders():
    assert p.build_state_report(volume=7) == p.build_volume_report(7)
    assert p.build_state_report(rgb=(9, 8, 7)) == p.build_lightbar_report(9, 8, 7)


def test_state_report_needs_something_to_update():
    with pytest.raises(ValueError):
        p.build_state_report()


def test_state_report_zero_padding_everywhere_else():
    r = p.build_state_report(volume=0x30, rgb=(1, 2, 3))
    used = {0, 1, 2, 3, 3 + 5, 3 + 6, 3 + 7, 3 + 21}
    for i in range(74):
        if i not in used:
            assert r[i] == 0, i


# --- helpers for inspecting reports -----------------------------------------

def test_is_audio_report():
    assert p.is_audio_report(p.build_audio_report(0, _frames()))
    assert not p.is_audio_report(p.build_volume_report(1))
    assert not p.is_audio_report(b"")
    assert not p.is_audio_report(b"\x17" + b"\x00" * 10)  # right id, wrong length


def test_is_state_report():
    assert p.is_state_report(p.build_volume_report(1))
    assert not p.is_state_report(p.build_audio_report(0, _frames()))


def test_parse_audio_report_roundtrip():
    frames = _frames()
    parsed = p.parse_audio_report(p.build_audio_report(0xABCD, frames, target=p.TARGET_HEADSET))
    assert parsed.counter == 0xABCD
    assert parsed.target == p.TARGET_HEADSET
    assert parsed.frames == tuple(frames)
    assert parsed.crc_ok is True


def test_parse_audio_report_detects_corruption():
    r = bytearray(p.build_audio_report(1, _frames()))
    r[100] ^= 0xFF
    assert p.parse_audio_report(bytes(r)).crc_ok is False


def test_parse_audio_report_rejects_wrong_shape():
    with pytest.raises(ValueError):
        p.parse_audio_report(b"\x17" * 10)
    with pytest.raises(ValueError):
        p.parse_audio_report(p.build_volume_report(1))
