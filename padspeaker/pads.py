"""Discovery of Bluetooth DualShock 4 style pads through sysfs.

A pad is a ``/dev/hidrawN`` node whose ``/sys/class/hidraw/hidrawN/device/uevent``
reports a Bluetooth bus (0005) and a known DS4 vendor and product id: the Sony ids
built in below, plus any ``VVVV:PPPP`` ids the user added with ``extra_pad_ids``
for DS4-compatible pads that report other ids.  The pad's MAC address is taken from ``HID_UNIQ``.  The kernel LED class
devices that belong to the pad (its lightbar) are found by matching the pad's HID
device directory name against the ``/sys/class/leds`` symlink targets.

Everything here only reads files below an injectable sysfs root, so it runs under
a fake tree in tests.  There are no threads; :class:`PadWatcher` is polled by
the caller.
"""
from __future__ import annotations

import ctypes
import os
import re
from dataclasses import dataclass
from typing import Callable, Dict, Iterable, List, Optional, Sequence, Tuple

BUS_BLUETOOTH = 0x0005
VENDOR_SONY = 0x054C
#: Known DS4 product ids: v1, v2 and the Sony USB dongle (which also reports DS4 pads).
DS4_PRODUCT_IDS = frozenset({0x05C4, 0x09CC, 0x0BA0})

#: The same ids as ``VVVV:PPPP`` text, in the order ``list`` shows them.
BUILTIN_PAD_IDS = ("054C:05C4", "054C:09CC", "054C:0BA0")

DEFAULT_NAME = "Wireless Controller"

_MAC_RE = re.compile(r"^[0-9a-f]{2}(:[0-9a-f]{2}){5}$")
_HID_ID_RE = re.compile(r"^([0-9A-Fa-f]{1,8}):([0-9A-Fa-f]{1,8}):([0-9A-Fa-f]{1,8})$")
_HIDRAW_RE = re.compile(r"^hidraw(\d+)$")
_PAD_ID_RE = re.compile(r"^([0-9A-Fa-f]{4}):([0-9A-Fa-f]{4})$")

# Linux input key codes that mark a gamepad (BTN_JOYSTICK starts the joystick range,
# BTN_GAMEPAD is the first gamepad button).
_BTN_JOYSTICK = 0x120
_BTN_GAMEPAD = 0x130
_BUTTON_RANGE_MASK = (1 << (_BTN_GAMEPAD - _BTN_JOYSTICK + 16)) - 1


@dataclass(frozen=True)
class Pad:
    """A connected Bluetooth DS4 style pad.

    Attributes:
        hidraw_path: The ``/dev/hidrawN`` node reports are written to.
        mac: Lower-case colon separated Bluetooth address.
        name: The pad's HID name (usually ``Wireless Controller``).
        hid_id: ``BBBB:VVVV:PPPP`` (bus, vendor, product) in upper-case hex.
        led_dir: Directory holding the pad's ``<prefix>:red|green|blue`` LED class
            devices, or None when the kernel exposes no lightbar for it.
    """

    hidraw_path: str
    mac: str
    name: str
    hid_id: str
    led_dir: Optional[str] = None
    extra_id: bool = False
    """True when the pad is only recognised because its id is in ``extra_pad_ids``."""


@dataclass(frozen=True)
class OtherDevice:
    """A Bluetooth gamepad that was not recognised (reported by ``check``)."""

    hidraw_path: str
    mac: str
    name: str
    hid_id: str
    """``BBBB:VVVV:PPPP`` (bus, vendor, product) in upper-case hex."""
    pad_id: str
    """``VVVV:PPPP``: what to put in ``extra_pad_ids`` if the pad is DS4-compatible."""


def normalize_pad_id(value: object) -> str:
    """Return ``value`` as an upper-case ``VVVV:PPPP`` hex id.

    Raises:
        ValueError: if it is not exactly two groups of four hex digits.
    """
    match = _PAD_ID_RE.match(value.strip()) if isinstance(value, str) else None
    if not match:
        raise ValueError("%r is not a pad id; use the vendor and product id as eight hex digits "
                         "such as 054C:09CC" % (value,))
    return "%s:%s" % (match.group(1).upper(), match.group(2).upper())


def _id_set(extra_ids: Optional[Iterable[str]]) -> frozenset:
    ids = {(int(i[:4], 16), int(i[5:], 16)) for i in map(normalize_pad_id, extra_ids or ())}
    return frozenset(ids)


def recognised_ids(extra_ids: Optional[Iterable[str]] = None) -> List[Tuple[str, str]]:
    """Every id the daemon accepts as ``(VVVV:PPPP, source)``; source is ``built-in`` or ``config``."""
    result = [(i, "built-in") for i in BUILTIN_PAD_IDS]
    seen = set(BUILTIN_PAD_IDS)
    for item in extra_ids or ():
        pad_id = normalize_pad_id(item)
        if pad_id not in seen:
            seen.add(pad_id)
            result.append((pad_id, "config"))
    return result


def mac_tail(mac: str) -> str:
    """Return the last four hex digits of ``mac`` in lower case (``1122``).

    Used to build per-pad sink names.  Raises ValueError when ``mac`` holds
    fewer than four hex digits.
    """
    digits = re.sub(r"[^0-9a-fA-F]", "", mac or "")
    if len(digits) < 4:
        raise ValueError("not a MAC address: %r" % (mac,))
    return digits[-4:].lower()


def _read_uevent(path: str) -> Dict[str, str]:
    """Parse a uevent file into a dict; return {} when it cannot be read."""
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            text = handle.read()
    except OSError:
        return {}
    values: Dict[str, str] = {}
    for line in text.splitlines():
        key, sep, value = line.partition("=")
        if sep and key:
            values[key.strip()] = value.strip()
    return values


def _parse_hid_id(text: str) -> Optional[Tuple[int, int, int]]:
    match = _HID_ID_RE.match(text or "")
    if not match:
        return None
    bus, vendor, product = (int(group, 16) for group in match.groups())
    return bus, vendor, product


def _find_led_dir(sysfs_root: str, hid_component: str) -> Optional[str]:
    """Find the directory with the LED class devices of one HID device."""
    leds_class = os.path.join(sysfs_root, "class", "leds")
    try:
        entries = sorted(os.listdir(leds_class))
    except OSError:
        return None
    for entry in entries:
        real = os.path.realpath(os.path.join(leds_class, entry))
        if hid_component in real.split(os.sep):
            return os.path.dirname(real)
    return None


def discover_pads(sysfs_root: str = "/sys", dev_root: str = "/dev",
                  extra_ids: Optional[Iterable[str]] = None) -> List[Pad]:
    """List the connected Bluetooth DS4 pads, sorted by MAC address.

    Only Bluetooth (bus 0005) devices with a valid ``HID_UNIQ`` MAC and either a
    built-in Sony DS4 id or one of ``extra_ids`` (``VVVV:PPPP`` text) are
    returned.  The extra ids never apply to other buses.  Unreadable or
    malformed entries are skipped silently.

    Raises:
        ValueError: if an entry of ``extra_ids`` is not a ``VVVV:PPPP`` id.
    """
    extra = _id_set(extra_ids)
    hidraw_class = os.path.join(sysfs_root, "class", "hidraw")
    try:
        names = os.listdir(hidraw_class)
    except OSError:
        return []
    pads: List[Pad] = []
    for node in names:
        if not _HIDRAW_RE.match(node):
            continue
        device = os.path.join(hidraw_class, node, "device")
        info = _read_uevent(os.path.join(device, "uevent"))
        parsed = _parse_hid_id(info.get("HID_ID", ""))
        if parsed is None:
            continue
        bus, vendor, product = parsed
        if bus != BUS_BLUETOOTH:
            continue
        builtin = vendor == VENDOR_SONY and product in DS4_PRODUCT_IDS
        if not builtin and (vendor, product) not in extra:
            continue
        mac = info.get("HID_UNIQ", "").lower()
        if not _MAC_RE.match(mac):
            continue
        hid_component = os.path.basename(os.path.realpath(device))
        pads.append(Pad(
            hidraw_path=os.path.join(dev_root, node),
            mac=mac,
            name=info.get("HID_NAME") or DEFAULT_NAME,
            hid_id="%04X:%04X:%04X" % (bus, vendor, product),
            led_dir=_find_led_dir(sysfs_root, hid_component),
            extra_id=not builtin,
        ))
    pads.sort(key=lambda pad: (pad.mac, pad.hidraw_path))
    return pads


def _is_gamepad(device: str, long_bits: int) -> bool:
    """True if an input node below the HID device ``device`` has gamepad or joystick buttons."""
    input_dir = os.path.join(device, "input")
    try:
        nodes = sorted(os.listdir(input_dir))
    except OSError:
        return False
    for node in nodes:
        try:
            with open(os.path.join(input_dir, node, "capabilities", "key"), "r") as handle:
                words = handle.read().split()
            values = [int(word, 16) for word in words]
        except (OSError, ValueError):
            continue
        # the kernel prints the most significant word first; each word is one C long
        bits = 0
        for value in values:
            bits = (bits << long_bits) | value
        # any button in 0x120..0x13f: the joystick and the gamepad ranges
        if (bits >> _BTN_JOYSTICK) & _BUTTON_RANGE_MASK:
            return True
    return False


def list_unrecognised_gamepads(sysfs_root: str = "/sys", dev_root: str = "/dev",
                               extra_ids: Optional[Iterable[str]] = None,
                               long_bits: Optional[int] = None) -> List[OtherDevice]:
    """List Bluetooth gamepads that :func:`discover_pads` does *not* recognise.

    This is the hint behind ``check``: a pad that is DS4-compatible but reports
    other ids shows up here with the ``VVVV:PPPP`` to add to ``extra_pad_ids``.
    A device counts as a gamepad when one of its input nodes has joystick or
    gamepad buttons.  Strictly read-only: it reads sysfs files and never opens
    a device node.

    Args:
        long_bits: bit width of the C ``long`` that the kernel uses in
            ``capabilities/key`` (default: this machine's).
    """
    bits = long_bits or ctypes.sizeof(ctypes.c_ulong) * 8
    known = {p.hidraw_path for p in discover_pads(sysfs_root, dev_root, extra_ids)}
    hidraw_class = os.path.join(sysfs_root, "class", "hidraw")
    try:
        names = os.listdir(hidraw_class)
    except OSError:
        return []
    found: List[OtherDevice] = []
    for node in sorted(names):
        if not _HIDRAW_RE.match(node):
            continue
        path = os.path.join(dev_root, node)
        device = os.path.join(hidraw_class, node, "device")
        info = _read_uevent(os.path.join(device, "uevent"))
        parsed = _parse_hid_id(info.get("HID_ID", ""))
        if parsed is None or parsed[0] != BUS_BLUETOOTH or path in known:
            continue
        if not _is_gamepad(os.path.realpath(device), bits):
            continue
        bus, vendor, product = parsed
        mac = info.get("HID_UNIQ", "").lower()
        found.append(OtherDevice(
            hidraw_path=path, mac=mac if _MAC_RE.match(mac) else "",
            name=info.get("HID_NAME") or "unnamed device",
            hid_id="%04X:%04X:%04X" % (bus, vendor, product),
            pad_id="%04X:%04X" % (vendor, product)))
    return found


class PadWatcher:
    """Detect pads appearing and disappearing between successive polls.

    Call :meth:`poll` periodically; it never blocks and starts no threads.
    A pad is identified by its MAC and hidraw node, so a pad that comes back
    on another node is reported as removed and added.
    """

    def __init__(self,
                 discover: Optional[Callable[[], List[Pad]]] = None,
                 sysfs_root: str = "/sys",
                 dev_root: str = "/dev",
                 extra_ids: Optional[Sequence[str]] = None) -> None:
        if discover is None:
            extra = list(extra_ids or ())

            def discover() -> List[Pad]:
                return discover_pads(sysfs_root, dev_root, extra)
        self._discover = discover
        self._known: List[Pad] = []

    @property
    def pads(self) -> List[Pad]:
        """The pads seen by the last poll."""
        return list(self._known)

    def poll(self) -> Tuple[List[Pad], List[Pad]]:
        """Return ``(added, removed)`` pads since the previous poll."""
        current = list(self._discover())
        old_keys = {(p.mac, p.hidraw_path) for p in self._known}
        new_keys = {(p.mac, p.hidraw_path) for p in current}
        added = [p for p in current if (p.mac, p.hidraw_path) not in old_keys]
        removed = [p for p in self._known if (p.mac, p.hidraw_path) not in new_keys]
        self._known = current
        return added, removed
