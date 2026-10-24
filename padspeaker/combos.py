"""Combined outputs: play to several speakers at once.

Besides one virtual sink per pad (``padspeaker_<tail>``) the daemon can create
combined sinks (``module-combine-sink``) that play to several sinks at once:

=========================== ================================== ==========================================
name                        description                        plays to
=========================== ================================== ==========================================
``padspeaker_all``          Controller speakers (all)          every pad sink (only with 2 or more pads)
``padspeaker_tv_<tail>``    <label> + controller speaker (...) the main sink and that pad's sink
``padspeaker_tv_all``       <label> + all controller speakers  the main sink and every pad sink (2+ pads)
=========================== ================================== ==========================================

The slave list of a combined sink is fixed when it is created, so whenever the
set of pads (or the main output) changes, :class:`ComboManager` removes the combos
whose slaves changed and creates them again.

Safety against feedback: a combo's slaves are only pad sinks (null sinks that
nothing but the daemon's capture reads) and the main output sink (often a TV), which must not be one
of our own sinks.  A combo is never the slave of another combo, so audio can
not circulate; :func:`validate_plan` checks this and the daemon refuses any
plan that fails it.  The daemon only ever captures a pad's *own* sink monitor.

Everything here goes through an injectable sink manager and does not raise on
audio server errors.
"""
from __future__ import annotations

import logging
import re
import unicodedata
from dataclasses import dataclass
from typing import Any, Callable, Collection, Dict, List, Mapping, Optional, Sequence, Tuple

__all__ = [
    "COMBO_ALL", "TV_ALL", "ALL_PREFIX", "TV_PREFIX", "PAD_PREFIX", "LEGACY_PREFIXES",
    "PadSink", "ComboFlags", "ComboSpec", "ComboManager",
    "our_prefixes", "is_ours", "plan_combos", "validate_plan",
    "sanitize_label", "resolve_label", "derive_label", "MAX_LABEL", "DEFAULT_LABEL",
]

log = logging.getLogger("padspeaker.combos")

PAD_PREFIX = "padspeaker_"
ALL_PREFIX = "padspeaker_all"
TV_PREFIX = "padspeaker_tv_"
COMBO_ALL = "padspeaker_all"
TV_ALL = "padspeaker_tv_all"
LEGACY_PREFIXES = ("ds4_speaker_", "ds4_speakers_", "ds4_tv_")
"""Sink name prefixes of releases before the project was renamed (``ds4-bt-speaker``).  The daemon never creates
these any more, but it still recognises them as its own: a stale one left over by the old version is cleaned up and
a saved Batocera audio choice that names one is reset like any other of our sinks."""


MAX_LABEL = 60
"""Longest label (characters) put into a combined output's description."""
DEFAULT_LABEL = "TV"
"""Label used when the main output and its description are both unknown."""

_DROPPED = {'"', "'", "\\", "`"}


def sanitize_label(text: Any) -> Optional[str]:
    """Make ``text`` safe to put in a sink description, or return None if nothing is left.

    Quotes, backslashes and control characters are removed (the text ends up
    inside ``sink_properties`` for ``pactl``), runs of white space become one
    space and the result is cut to :data:`MAX_LABEL` characters.
    """
    if not isinstance(text, str):
        return None
    kept = []
    for ch in text:
        if ch.isspace():
            kept.append(" ")
        elif ch in _DROPPED or unicodedata.category(ch) in ("Cc", "Cf", "Cs", "Co", "Cn"):
            continue
        else:
            kept.append(ch)
    cleaned = " ".join("".join(kept).split())[:MAX_LABEL].rstrip()
    return cleaned or None


_GENERIC_NAMES = frozenset({
    "default", "analog", "digital", "speaker", "speakers", "headphones", "headset", "line out",
    "line", "front", "pcm", "spdif", "s/pdif", "iec958", "hd-audio generic", "hda intel pch",
    "hda intel", "alsa", "pipewire", "pulseaudio", "audio", "sink", "output",
})
_GENERIC_RE = re.compile(r"^(hdmi|displayport|display port|dp)( ?/? ?(hdmi|displayport))?( ?\d+)?$")
_PORT_RE = re.compile(r"\(([^()]+)\)\s*$")
_NAME_KEYS = ("node.nick", "alsa.name")
_CARD_KEYS = ("device.description", "alsa.card_name", "device.product.name")


def derive_label(description: str, properties: Optional[Mapping[str, str]] = None) -> Optional[str]:
    """Name the main output the way the output itself does.

    A monitor or TV reports its own name (from its EDID) as ``node.nick`` or
    ``alsa.name``, for example ``Example Display``.  That is used, followed by the
    parenthesised port from the sink's description when there is one:
    ``Example Display (HDMI 2)``.  Generic values (``HDMI``, ``default``, ``Analog``,
    the sound card's own name...) are ignored.  Without a usable name the
    description is returned; None only when both are empty.
    """
    props = properties or {}
    cards = {(props.get(k) or "").strip().lower() for k in _CARD_KEYS} - {""}
    for key in _NAME_KEYS:
        nick = sanitize_label(props.get(key))
        if not nick:
            continue
        low = nick.lower()
        if low in _GENERIC_NAMES or _GENERIC_RE.match(low) or low in cards:
            continue
        port = _PORT_RE.search(description or "")
        if port and "(" not in nick:
            nick = "%s (%s)" % (nick, port.group(1).strip())
        return sanitize_label(nick)
    return sanitize_label(description)


def resolve_label(configured: Optional[str], main_sink: Optional[str],
                  descriptions: Mapping[str, str],
                  properties: Optional[Mapping[str, Mapping[str, str]]] = None) -> str:
    """Choose the wording used for the main output in the combined outputs' descriptions.

    Priority: (1) the configured ``tv_label``; (2) the output's own name from
    its ``node.nick`` / ``alsa.name`` (see :func:`derive_label`); (3) its
    description from the audio server; (4) its sink name; (5) :data:`DEFAULT_LABEL`.
    """
    label = sanitize_label(configured)
    if label:
        return label
    if main_sink:
        props = (properties or {}).get(main_sink)
        return (derive_label(descriptions.get(main_sink, ""), props)
                or sanitize_label(main_sink) or DEFAULT_LABEL)
    return DEFAULT_LABEL


@dataclass(frozen=True)
class PadSink:
    """A connected pad and the name of its virtual sink."""

    mac: str
    name: str
    sink: str

    @property
    def tail(self) -> str:
        return self.mac.replace(":", "")[-4:].lower()


@dataclass(frozen=True)
class ComboFlags:
    """Which groups of combined outputs to create."""

    pads: bool = True
    """``padspeaker_all``"""
    tv_pad: bool = True
    """``padspeaker_tv_<tail>``, one per pad"""
    tv_all: bool = True
    """``padspeaker_tv_all``"""


@dataclass(frozen=True)
class ComboSpec:
    """One combined output to create."""

    name: str
    description: str
    slaves: Tuple[str, ...]
    group: str


def our_prefixes(pad_prefix: str = PAD_PREFIX) -> Tuple[str, ...]:
    """Name prefixes of everything the daemon creates (pad sinks and both kinds of combo), followed by the
    :data:`LEGACY_PREFIXES` of the sinks the pre-rename version created."""
    return (pad_prefix, ALL_PREFIX, TV_PREFIX) + LEGACY_PREFIXES


def is_ours(name: str, pad_prefix: str = PAD_PREFIX) -> bool:
    """True if ``name`` is a sink the daemon creates."""
    return bool(name) and name.startswith(our_prefixes(pad_prefix))


def plan_combos(pads: Sequence[PadSink], tv_sink: Optional[str], flags: ComboFlags,
                pad_prefix: str = PAD_PREFIX, tv_label: str = DEFAULT_LABEL) -> List[ComboSpec]:
    """Return the combined outputs wanted for these pads and main (TV) sink.

    ``tv_label`` words the main output in the descriptions ("<label> + controller
    speaker (...)"); the sink names never depend on it.

    A TV sink that is one of ours is ignored (it would be a loop).  With a
    single pad ``padspeaker_tv_all`` would equal that pad's own TV combo, so it is
    skipped; ``padspeaker_all`` needs at least two pads.
    """
    ordered = sorted(pads, key=lambda p: p.mac)
    tv = tv_sink if tv_sink and not is_ours(tv_sink, pad_prefix) else None
    sinks = tuple(p.sink for p in ordered)
    specs: List[ComboSpec] = []
    if flags.pads and len(ordered) >= 2:
        specs.append(ComboSpec(COMBO_ALL, "Controller speakers (all)", sinks, "pads"))
    if tv:
        if flags.tv_pad:
            for pad in ordered:
                specs.append(ComboSpec(
                    TV_PREFIX + pad.tail,
                    "%s + controller speaker (%s %s)" % (tv_label, pad.name, pad.tail),
                    (tv, pad.sink), "tv_pad"))
        if flags.tv_all and len(ordered) >= 2:
            specs.append(ComboSpec(TV_ALL, "%s + all controller speakers" % tv_label,
                                  (tv,) + sinks, "tv_all"))
    return specs


def validate_plan(specs: Sequence[ComboSpec], pads: Sequence[PadSink], tv_sink: Optional[str],
                  pad_prefix: str = PAD_PREFIX) -> None:
    """Check that no plan can feed audio back into itself.

    Raises:
        ValueError: if the TV sink is one of ours, or a combo has a slave that
            is itself, another combo or something unknown.
    """
    if tv_sink and is_ours(tv_sink, pad_prefix):
        raise ValueError("the TV sink %r is one of our own sinks" % tv_sink)
    pad_sinks = {p.sink for p in pads}
    combos = {s.name for s in specs}
    allowed = pad_sinks | ({tv_sink} if tv_sink else set())
    for spec in specs:
        for slave in spec.slaves:
            if slave == spec.name:
                raise ValueError("combined output %s lists itself as a slave" % spec.name)
            if slave in combos:
                raise ValueError("combined output %s would play into another combined output (%s)"
                                 % (spec.name, slave))
            if slave not in allowed:
                raise ValueError("combined output %s has an unknown slave %r" % (spec.name, slave))


class ComboManager:
    """Creates, rebuilds and removes the combined sinks so they match the pads.

    Args:
        sinks: sink manager with ``create_combine_sink``, ``remove_null_sink``,
            ``default_sink`` (and, through ``fallback``, ``set_default_sink``).
        flags: which groups to create.
        fallback: ``fallback(previous_default)`` moves the default output away
            from a combo that is going away (and resets Batocera's saved choice).
        previous_default: returns the default output the user had before picking
            one of ours, or None.
        pad_prefix: prefix of the pad sink names.
    """

    def __init__(self, sinks: Any, flags: ComboFlags,
                 fallback: Callable[[Optional[str]], None],
                 previous_default: Callable[[], Optional[str]] = lambda: None,
                 pad_prefix: str = PAD_PREFIX) -> None:
        self._sinks = sinks
        self._flags = flags
        self._fallback = fallback
        self._previous_default = previous_default
        self._pad_prefix = pad_prefix
        self._active: Dict[str, Tuple[ComboSpec, Any]] = {}

    # -- queries --------------------------------------------------------------------

    def slaves_of(self) -> Dict[str, Tuple[str, ...]]:
        """``{combo name: slave sinks}`` of the combos that exist now."""
        return {name: spec.slaves for name, (spec, _) in self._active.items()}

    def snapshot(self) -> List[Dict[str, Any]]:
        """A JSON friendly list of the existing combos."""
        return [{"name": spec.name, "description": spec.description,
                 "slaves": list(spec.slaves), "group": spec.group}
                for spec, _ in self._active.values()]

    # -- changes --------------------------------------------------------------------

    def update(self, pads: Sequence[PadSink], tv_sink: Optional[str],
               present: Collection[str], tv_label: str = DEFAULT_LABEL) -> bool:
        """Make the existing combos match the pads and TV sink.

        Args:
            pads: pads whose sinks exist.
            tv_sink: the main (TV) sink, or None.
            tv_label: wording for it in the descriptions; a changed label
                rebuilds the combos that carry it (their names stay the same).
            present: names of all sinks that exist right now.

        Returns:
            True when everything planned exists; False when something had to be
            postponed (a slave sink is missing) or failed, so the caller should
            try again soon.  Never raises.
        """
        present = set(present)
        tv = tv_sink if tv_sink and not is_ours(tv_sink, self._pad_prefix) else None
        specs = plan_combos(pads, tv, self._flags, self._pad_prefix, tv_label)
        try:
            validate_plan(specs, pads, tv, self._pad_prefix)
        except ValueError as exc:
            log.error("refusing a combined output plan: %s", exc)
            specs = []
        complete = True
        desired: Dict[str, ComboSpec] = {}
        for spec in specs:
            missing = [s for s in spec.slaves if s not in present]
            if missing:
                log.debug("postponing %s: %s does not exist yet", spec.name, ", ".join(missing))
                complete = False
            else:
                desired[spec.name] = spec
        for name in [n for n in self._active if n not in present]:
            log.info("combined output %s disappeared by itself", name)
            del self._active[name]

        remove = [n for n, (spec, _) in self._active.items() if desired.get(n) != spec]
        remove += [n for n in desired if n in present and n not in self._active]   # unknown, older
        default = self._query_default() if remove else None
        keep_default = set()
        for name in remove:
            if default == name:
                if name in desired:
                    keep_default.add(name)
                else:
                    self._call_fallback()
            self._unload(name)
        for name, spec in desired.items():
            if name in self._active:
                continue
            try:
                module = self._sinks.create_combine_sink(name, spec.description, list(spec.slaves))
            except Exception as exc:
                log.warning("cannot create the combined output %s: %s", name, exc)
                complete = False
                continue
            self._active[name] = (spec, module)
            log.info("created combined output %s (%s)", name, ", ".join(spec.slaves))
            if name in keep_default:
                try:
                    self._sinks.set_default_sink(name)
                except Exception as exc:
                    log.warning("cannot make %s the default output again: %s", name, exc)
        return complete

    def remove_all(self) -> None:
        """Remove every combo; move the default away first if it is one of them."""
        if not self._active:
            return
        default = self._query_default()
        if default in self._active:
            self._call_fallback()
        for name in list(self._active):
            self._unload(name)

    # -- helpers ------------------------------------------------------------------------

    def _query_default(self) -> Optional[str]:
        try:
            return self._sinks.default_sink()
        except Exception as exc:
            log.debug("cannot read the default sink: %s", exc)
            return None

    def _call_fallback(self) -> None:
        try:
            self._fallback(self._previous_default())
        except Exception as exc:
            log.warning("could not restore the default output: %s", exc)

    def _unload(self, name: str) -> None:
        entry = self._active.pop(name, None)
        target = entry[1] if entry else name
        try:
            self._sinks.remove_null_sink(target)
            log.info("removed combined output %s", name)
        except Exception as exc:
            log.warning("cannot remove the combined output %s: %s", name, exc)
