"""Tests for padspeaker.pads using a fake sysfs tree."""
import os
from pathlib import Path

import pytest

from padspeaker.pads import Pad, PadWatcher, discover_pads, mac_tail

SONY_V2 = "0005:0000054C:000009CC"


def add_hidraw(sysfs, dev, n, hid_id=SONY_V2, name="Wireless Controller",
               uniq="aa:bb:cc:dd:11:22", hid_dir=None, uevent=None):
    """Create /sys/class/hidrawN/device -> HID device dir and a /dev node."""
    hid_dir = hid_dir or "0005:054C:09CC.%04d" % n
    real = sysfs / "devices" / "virtual" / "misc" / "uhid" / hid_dir
    real.mkdir(parents=True, exist_ok=True)
    if uevent is None:
        uevent = "DRIVER=playstation\nHID_ID=%s\nHID_NAME=%s\nHID_UNIQ=%s\nMODALIAS=x\n" % (
            hid_id, name, uniq)
    (real / "uevent").write_text(uevent)
    node = sysfs / "class" / "hidraw" / ("hidraw%d" % n)
    node.mkdir(parents=True, exist_ok=True)
    os.symlink(str(real), str(node / "device"))
    dev.mkdir(parents=True, exist_ok=True)
    (dev / ("hidraw%d" % n)).write_text("")
    return real


def add_leds(sysfs, hid_real, prefix="input20"):
    leds = hid_real / "leds"
    classdir = sysfs / "class" / "leds"
    classdir.mkdir(parents=True, exist_ok=True)
    for colour in ("red", "green", "blue", "global"):
        d = leds / ("%s:%s" % (prefix, colour))
        d.mkdir(parents=True)
        (d / "brightness").write_text("0\n")
        (d / "max_brightness").write_text("255\n")
        rel = os.path.relpath(str(d), str(classdir))
        os.symlink(rel, str(classdir / ("%s:%s" % (prefix, colour))))
    return leds


@pytest.fixture
def tree(tmp_path):
    return tmp_path / "sys", tmp_path / "dev"


def test_mac_tail():
    assert mac_tail("aa:bb:cc:dd:11:22") == "1122"
    assert mac_tail("aa-bb-cc-dd-11-22") == "1122"


def test_mac_tail_rejects_garbage():
    with pytest.raises(ValueError):
        mac_tail("")
    with pytest.raises(ValueError):
        mac_tail("zz")


def test_discovers_bluetooth_ds4(tree):
    sysfs, dev = tree
    real = add_hidraw(sysfs, dev, 3)
    leds = add_leds(sysfs, real)
    pads = discover_pads(sysfs_root=str(sysfs), dev_root=str(dev))
    assert len(pads) == 1
    pad = pads[0]
    assert pad == Pad(
        hidraw_path=str(dev / "hidraw3"), mac="aa:bb:cc:dd:11:22",
        name="Wireless Controller", hid_id="0005:054C:09CC",
        led_dir=str(leds.resolve()))


def test_pad_without_leds_has_no_led_dir(tree):
    sysfs, dev = tree
    add_hidraw(sysfs, dev, 3)
    (pad,) = discover_pads(str(sysfs), str(dev))
    assert pad.led_dir is None


def test_led_belongs_to_matching_pad_only(tree):
    sysfs, dev = tree
    a = add_hidraw(sysfs, dev, 1, uniq="aa:aa:aa:aa:aa:01", hid_dir="0005:054C:09CC.0001")
    b = add_hidraw(sysfs, dev, 2, uniq="aa:aa:aa:aa:aa:02", hid_dir="0005:054C:09CC.0002")
    add_leds(sysfs, a, prefix="input10")
    leds_b = add_leds(sysfs, b, prefix="input11")
    pads = discover_pads(str(sysfs), str(dev))
    assert [p.mac for p in pads] == ["aa:aa:aa:aa:aa:01", "aa:aa:aa:aa:aa:02"]
    assert pads[0].led_dir == str((a / "leds").resolve())
    assert pads[1].led_dir == str(leds_b.resolve())


def test_sorted_by_mac(tree):
    sysfs, dev = tree
    add_hidraw(sysfs, dev, 1, uniq="ff:00:00:00:00:01")
    add_hidraw(sysfs, dev, 2, uniq="00:00:00:00:00:09")
    add_hidraw(sysfs, dev, 10, uniq="aa:00:00:00:00:00")
    pads = discover_pads(str(sysfs), str(dev))
    assert [p.mac for p in pads] == [
        "00:00:00:00:00:09", "aa:00:00:00:00:00", "ff:00:00:00:00:01"]


def test_mac_is_lowercased(tree):
    sysfs, dev = tree
    add_hidraw(sysfs, dev, 1, uniq="aa:bb:cc:dd:11:22")
    (pad,) = discover_pads(str(sysfs), str(dev))
    assert pad.mac == "aa:bb:cc:dd:11:22"


@pytest.mark.parametrize("hid_id,product", [
    ("0005:0000054C:000005C4", "05C4"),
    ("0005:0000054C:000009CC", "09CC"),
    ("0005:0000054C:00000BA0", "0BA0"),
])
def test_known_product_ids(tree, hid_id, product):
    sysfs, dev = tree
    add_hidraw(sysfs, dev, 1, hid_id=hid_id)
    (pad,) = discover_pads(str(sysfs), str(dev))
    assert pad.hid_id == "0005:054C:" + product


def test_ignores_usb_sony_pad(tree):
    sysfs, dev = tree
    add_hidraw(sysfs, dev, 1, hid_id="0003:0000054C:000009CC", uniq="")
    assert discover_pads(str(sysfs), str(dev)) == []


def test_ignores_logitech_usb_devices(tree):
    sysfs, dev = tree
    add_hidraw(sysfs, dev, 1, hid_id="0003:0000046D:0000C52B", name="Logitech USB Receiver",
               uniq="")
    add_hidraw(sysfs, dev, 2, hid_id="0003:0000046D:0000C52B", name="Logitech USB Receiver",
               uniq="aa:bb:cc:dd:11:22")
    assert discover_pads(str(sysfs), str(dev)) == []


def test_ignores_bluetooth_non_sony_and_unknown_sony_product(tree):
    sysfs, dev = tree
    add_hidraw(sysfs, dev, 1, hid_id="0005:0000046D:000009CC")       # not Sony
    add_hidraw(sysfs, dev, 2, hid_id="0005:0000054C:00000CE6")       # DualSense, unsupported
    assert discover_pads(str(sysfs), str(dev)) == []


def test_tolerates_odd_entries(tree):
    sysfs, dev = tree
    add_hidraw(sysfs, dev, 1)
    add_hidraw(sysfs, dev, 2, uevent="")                              # empty
    add_hidraw(sysfs, dev, 3, uevent="HID_ID=garbage\nHID_UNIQ=x\n")  # malformed id
    add_hidraw(sysfs, dev, 4, uevent="\xff\xfe=\n\n===\nHID_ID")      # junk
    add_hidraw(sysfs, dev, 5, uniq="")                                # no MAC
    add_hidraw(sysfs, dev, 6, uniq="not-a-mac")
    (sysfs / "class" / "hidraw" / "hidraw7").mkdir()                  # no device link
    (sysfs / "class" / "hidraw" / "notes.txt").write_text("hi")       # a file
    (sysfs / "class" / "hidraw" / "hidrawX").mkdir()                  # bad number
    raw = add_hidraw(sysfs, dev, 8)
    (raw / "uevent").write_bytes(b"HID_ID=0005:0000054C:000009CC\n\xff\xfe\x00\n")
    pads = discover_pads(str(sysfs), str(dev))
    assert [p.hidraw_path for p in pads if p.hidraw_path.endswith("hidraw1")]
    assert all(p.hid_id == "0005:054C:09CC" for p in pads)


def test_missing_sysfs_returns_empty(tree):
    sysfs, dev = tree
    assert discover_pads(str(sysfs), str(dev)) == []


def test_unreadable_uevent_is_skipped(tree):
    sysfs, dev = tree
    real = add_hidraw(sysfs, dev, 1)
    (real / "uevent").unlink()
    (real / "uevent").mkdir()                  # reading a directory raises OSError
    assert discover_pads(str(sysfs), str(dev)) == []


def test_missing_name_defaults(tree):
    sysfs, dev = tree
    add_hidraw(sysfs, dev, 1, uevent="HID_ID=%s\nHID_UNIQ=aa:bb:cc:dd:11:22\n" % SONY_V2)
    (pad,) = discover_pads(str(sysfs), str(dev))
    assert pad.name == "Wireless Controller"


def _pad(n, mac):
    return Pad("/dev/hidraw%d" % n, mac, "Wireless Controller", "0005:054C:09CC", None)


def test_watcher_reports_added_and_removed():
    a, b = _pad(1, "00:00:00:00:00:01"), _pad(2, "00:00:00:00:00:02")
    states = [[a], [a, b], [b], [b], []]
    it = iter(states)
    w = PadWatcher(discover=lambda: next(it))
    assert w.poll() == ([a], [])
    assert w.poll() == ([b], [])
    assert w.poll() == ([], [a])
    assert w.poll() == ([], [])
    assert w.poll() == ([], [b])


def test_watcher_node_change_is_remove_and_add():
    old, new = _pad(1, "00:00:00:00:00:01"), _pad(4, "00:00:00:00:00:01")
    it = iter([[old], [new]])
    w = PadWatcher(discover=lambda: next(it))
    w.poll()
    assert w.poll() == ([new], [old])


def test_watcher_exposes_current_pads():
    a = _pad(1, "00:00:00:00:00:01")
    w = PadWatcher(discover=lambda: [a])
    assert w.pads == []
    w.poll()
    assert w.pads == [a]


def test_watcher_default_discover_uses_roots(tree):
    sysfs, dev = tree
    add_hidraw(sysfs, dev, 1)
    w = PadWatcher(sysfs_root=str(sysfs), dev_root=str(dev))
    added, removed = w.poll()
    assert [p.mac for p in added] == ["aa:bb:cc:dd:11:22"] and removed == []


# --- extra pad ids (config: extra_pad_ids) ------------------------------------------

from padspeaker import pads as pads_module  # noqa: E402
from padspeaker.pads import (BUILTIN_PAD_IDS, list_unrecognised_gamepads,  # noqa: E402
                             normalize_pad_id, recognised_ids)

CLONE = "0005:00001234:0000ABCD"          # a made-up DS4-compatible clone reporting its own ids


def test_normalize_pad_id_accepts_vendor_product_in_any_case():
    assert normalize_pad_id("1234:abcd") == "1234:ABCD"
    assert normalize_pad_id(" 054c:09cc ") == "054C:09CC"
    assert normalize_pad_id("0001:0002") == "0001:0002"


@pytest.mark.parametrize("bad", ["", "1234", "1234:abc", "1234:abcde", "0005:1234:abcd", "12:34:56",
                                 "zzzz:1234", "1234-abcd", "0x1234:0xabcd", None, 5, ["1234:abcd"]])
def test_normalize_pad_id_rejects_everything_else(bad):
    with pytest.raises(ValueError):
        normalize_pad_id(bad)


def test_recognised_ids_lists_builtin_then_extra_with_their_source():
    ids = recognised_ids(["1234:abcd", "054C:09CC", "1234:ABCD"])
    assert [i for i, _ in ids[:3]] == ["054C:05C4", "054C:09CC", "054C:0BA0"]
    assert [s for _, s in ids[:3]] == ["built-in"] * 3
    assert ids[3:] == [("1234:ABCD", "config")]          # duplicates and built-in ids are not repeated
    assert len(BUILTIN_PAD_IDS) == 3


def test_extra_id_is_not_recognised_unless_configured(tree):
    sysfs, dev = tree
    add_hidraw(sysfs, dev, 1, hid_id=CLONE, name="Clone Pad", uniq="aa:bb:cc:dd:ee:01")
    assert discover_pads(str(sysfs), str(dev)) == []
    found = discover_pads(str(sysfs), str(dev), extra_ids=["1234:abcd"])
    assert len(found) == 1
    pad = found[0]
    assert pad.mac == "aa:bb:cc:dd:ee:01" and pad.name == "Clone Pad"
    assert pad.hid_id == "0005:1234:ABCD" and pad.extra_id is True


def test_builtin_pads_are_not_marked_as_extra(tree):
    sysfs, dev = tree
    add_hidraw(sysfs, dev, 1)
    add_hidraw(sysfs, dev, 2, hid_id=CLONE, uniq="aa:bb:cc:dd:ee:02")
    found = {p.mac: p for p in discover_pads(str(sysfs), str(dev), extra_ids=["1234:ABCD", "054C:09CC"])}
    assert found["aa:bb:cc:dd:11:22"].extra_id is False
    assert found["aa:bb:cc:dd:ee:02"].extra_id is True


def test_extra_ids_only_apply_to_the_bluetooth_bus(tree):
    sysfs, dev = tree
    add_hidraw(sysfs, dev, 1, hid_id="0003:00001234:0000ABCD", uniq="aa:bb:cc:dd:ee:03")     # USB
    add_hidraw(sysfs, dev, 2, hid_id="0019:00001234:0000ABCD", uniq="aa:bb:cc:dd:ee:04")     # host bus
    assert discover_pads(str(sysfs), str(dev), extra_ids=["1234:ABCD"]) == []


def test_extra_ids_do_not_match_other_products(tree):
    sysfs, dev = tree
    add_hidraw(sysfs, dev, 1, hid_id="0005:00001234:0000ABCE", uniq="aa:bb:cc:dd:ee:05")
    assert discover_pads(str(sysfs), str(dev), extra_ids=["1234:ABCD"]) == []


def test_extra_ids_still_need_a_valid_mac(tree):
    sysfs, dev = tree
    add_hidraw(sysfs, dev, 1, hid_id=CLONE, uniq="")
    assert discover_pads(str(sysfs), str(dev), extra_ids=["1234:ABCD"]) == []


def test_discover_pads_rejects_a_malformed_extra_id(tree):
    sysfs, dev = tree
    with pytest.raises(ValueError):
        discover_pads(str(sysfs), str(dev), extra_ids=["nonsense"])


def test_watcher_passes_extra_ids_to_discovery(tree):
    sysfs, dev = tree
    add_hidraw(sysfs, dev, 1, hid_id=CLONE, uniq="aa:bb:cc:dd:ee:06")
    plain = PadWatcher(sysfs_root=str(sysfs), dev_root=str(dev))
    assert plain.poll() == ([], [])
    extra = PadWatcher(sysfs_root=str(sysfs), dev_root=str(dev), extra_ids=["1234:abcd"])
    added, removed = extra.poll()
    assert [p.mac for p in added] == ["aa:bb:cc:dd:ee:06"] and removed == []


# --- the safe "what did you not recognise" hint ---------------------------------------

LONG_BITS = 64


def key_caps(bits, long_bits=LONG_BITS):
    """The text the kernel puts in /sys/.../capabilities/key for these key codes."""
    words = {}
    for bit in bits:
        words[bit // long_bits] = words.get(bit // long_bits, 0) | (1 << (bit % long_bits))
    top = max(words) if words else 0
    return " ".join("%x" % words.get(i, 0) for i in range(top, -1, -1)) + "\n"


def add_input(real, number, name, key_bits, long_bits=LONG_BITS):
    node = real / "input" / ("input%d" % number)
    (node / "capabilities").mkdir(parents=True)
    (node / "name").write_text(name + "\n")
    (node / "capabilities" / "key").write_text(key_caps(key_bits, long_bits))
    return node


BTN_GAMEPAD = 0x130
BTN_JOYSTICK = 0x120
KEY_A = 30


def test_unrecognised_gamepads_are_listed_with_their_ids(tree):
    sysfs, dev = tree
    real = add_hidraw(sysfs, dev, 1, hid_id="0005:00002DC8:00003106", name="Other Pad",
                      uniq="aa:bb:cc:dd:ee:07", hid_dir="0005:2DC8:3106.0001")
    add_input(real, 5, "Other Pad", [BTN_GAMEPAD, 0x131])
    found = list_unrecognised_gamepads(str(sysfs), str(dev), long_bits=LONG_BITS)
    assert len(found) == 1
    item = found[0]
    assert item.pad_id == "2DC8:3106" and item.hid_id == "0005:2DC8:3106"
    assert item.name == "Other Pad" and item.mac == "aa:bb:cc:dd:ee:07"
    assert item.hidraw_path == str(dev / "hidraw1")


def test_joystick_button_range_counts_as_a_gamepad_too(tree):
    sysfs, dev = tree
    real = add_hidraw(sysfs, dev, 1, hid_id="0005:00002DC8:00003106", uniq="aa:bb:cc:dd:ee:07")
    add_input(real, 5, "Stick", [BTN_JOYSTICK])
    assert len(list_unrecognised_gamepads(str(sysfs), str(dev), long_bits=LONG_BITS)) == 1


def test_the_hint_works_with_32_bit_bitmaps(tree):
    sysfs, dev = tree
    real = add_hidraw(sysfs, dev, 1, hid_id="0005:00002DC8:00003106", uniq="aa:bb:cc:dd:ee:07")
    add_input(real, 5, "Pad32", [BTN_GAMEPAD], long_bits=32)
    assert len(list_unrecognised_gamepads(str(sysfs), str(dev), long_bits=32)) == 1


def test_recognised_keyboards_usb_and_input_less_devices_are_not_in_the_hint(tree):
    sysfs, dev = tree
    # recognised DS4 (also has gamepad buttons)
    ds4 = add_hidraw(sysfs, dev, 1)
    add_input(ds4, 5, "Wireless Controller", [BTN_GAMEPAD])
    # a Bluetooth keyboard
    kb = add_hidraw(sysfs, dev, 2, hid_id="0005:0000046D:0000B342", name="Keyboard", uniq="aa:bb:cc:dd:ee:08",
                    hid_dir="0005:046D:B342.0002")
    add_input(kb, 6, "Keyboard", [KEY_A])
    # a USB gamepad
    usb = add_hidraw(sysfs, dev, 3, hid_id="0003:00002DC8:00003106", uniq="", hid_dir="0003:2DC8:3106.0003")
    add_input(usb, 7, "USB pad", [BTN_GAMEPAD])
    # a Bluetooth HID device without any input node
    add_hidraw(sysfs, dev, 4, hid_id="0005:00001111:00002222", uniq="aa:bb:cc:dd:ee:09", hid_dir="0005:1111:2222.0004")
    assert list_unrecognised_gamepads(str(sysfs), str(dev), long_bits=LONG_BITS) == []


def test_a_configured_extra_id_removes_the_device_from_the_hint(tree):
    sysfs, dev = tree
    real = add_hidraw(sysfs, dev, 1, hid_id=CLONE, uniq="aa:bb:cc:dd:ee:0a", hid_dir="0005:1234:ABCD.0001")
    add_input(real, 5, "Clone", [BTN_GAMEPAD])
    assert len(list_unrecognised_gamepads(str(sysfs), str(dev), long_bits=LONG_BITS)) == 1
    assert list_unrecognised_gamepads(str(sysfs), str(dev), extra_ids=["1234:abcd"], long_bits=LONG_BITS) == []


def test_the_hint_survives_unreadable_or_odd_files(tree):
    sysfs, dev = tree
    real = add_hidraw(sysfs, dev, 1, hid_id="0005:00002DC8:00003106", uniq="aa:bb:cc:dd:ee:07")
    node = add_input(real, 5, "Odd", [BTN_GAMEPAD])
    (node / "capabilities" / "key").write_text("not hex at all\n")
    assert list_unrecognised_gamepads(str(sysfs), str(dev), long_bits=LONG_BITS) == []
    assert list_unrecognised_gamepads(str(sysfs / "missing"), str(dev), long_bits=LONG_BITS) == []


def test_the_hint_only_reads(tree, monkeypatch):
    """Listing other gamepads must never open a file for writing (and never touch /dev)."""
    sysfs, dev = tree
    real = add_hidraw(sysfs, dev, 1, hid_id="0005:00002DC8:00003106", uniq="aa:bb:cc:dd:ee:07")
    add_input(real, 5, "Other Pad", [BTN_GAMEPAD])
    import builtins
    real_open = builtins.open
    modes = []

    def spy(file, mode="r", *a, **k):
        modes.append(mode)
        return real_open(file, mode, *a, **k)
    monkeypatch.setattr(builtins, "open", spy)
    monkeypatch.setattr(os, "open", lambda *a, **k: pytest.fail("os.open must not be used"))
    assert len(list_unrecognised_gamepads(str(sysfs), str(dev), long_bits=LONG_BITS)) == 1
    assert modes and set(modes) == {"r"}
