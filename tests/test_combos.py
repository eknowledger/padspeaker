"""Tests for padspeaker.combos: planning the combined outputs and keeping them in step."""
import itertools

import pytest

from padspeaker.combos import (ALL_PREFIX, COMBO_ALL, LEGACY_PREFIXES, PAD_PREFIX, TV_ALL, TV_PREFIX,
                               ComboFlags, ComboManager, ComboSpec, PadSink, is_ours, our_prefixes, plan_combos, resolve_label,
                               sanitize_label, validate_plan, derive_label)
from padspeaker.sinks import SinkError

TV = "alsa_output.hdmi-stereo"
A = PadSink("aa:bb:cc:dd:11:22", "Wireless Controller", "padspeaker_1122")
B = PadSink("aa:bb:cc:dd:ee:ff", "Wireless Controller", "padspeaker_eeff")
C = PadSink("11:22:33:44:55:66", "Wireless Controller", "padspeaker_5566")


def names(specs):
    return [s.name for s in specs]


# --- planning --------------------------------------------------------------------------

def test_one_pad_with_tv_gets_only_the_tv_pad_combo():
    specs = plan_combos([A], TV, ComboFlags())
    assert names(specs) == ["padspeaker_tv_1122"]
    assert specs[0].slaves == (TV, "padspeaker_1122")
    assert specs[0].description == "TV + controller speaker (Wireless Controller 1122)"
    assert specs[0].group == "tv_pad"


def test_two_pads_with_tv_get_all_four_combos():
    specs = {s.name: s for s in plan_combos([B, A], TV, ComboFlags())}
    assert set(specs) == {"padspeaker_all", "padspeaker_tv_1122", "padspeaker_tv_eeff", "padspeaker_tv_all"}
    assert specs["padspeaker_all"].slaves == ("padspeaker_1122", "padspeaker_eeff")
    assert specs["padspeaker_all"].description == "Controller speakers (all)"
    assert specs["padspeaker_all"].group == "pads"
    assert specs["padspeaker_tv_all"].slaves == (TV, "padspeaker_1122", "padspeaker_eeff")
    assert specs["padspeaker_tv_all"].description == "TV + all controller speakers"
    assert specs["padspeaker_tv_eeff"].slaves == (TV, "padspeaker_eeff")


def test_two_pads_without_tv_only_get_the_pads_combo():
    assert names(plan_combos([A, B], None, ComboFlags())) == ["padspeaker_all"]


def test_nothing_with_no_pads_or_a_single_pad_without_tv():
    assert plan_combos([], TV, ComboFlags()) == []
    assert plan_combos([A], None, ComboFlags()) == []


def test_each_group_can_be_switched_off():
    base = {"padspeaker_all", "padspeaker_tv_1122", "padspeaker_tv_eeff", "padspeaker_tv_all"}
    assert set(names(plan_combos([A, B], TV, ComboFlags(pads=False)))) == base - {"padspeaker_all"}
    assert set(names(plan_combos([A, B], TV, ComboFlags(tv_pad=False)))) == {"padspeaker_all",
                                                                              "padspeaker_tv_all"}
    assert set(names(plan_combos([A, B], TV, ComboFlags(tv_all=False)))) == base - {"padspeaker_tv_all"}
    assert plan_combos([A, B], TV, ComboFlags(False, False, False)) == []


def test_ours_means_all_three_prefixes():
    assert our_prefixes("padspeaker_") == ("padspeaker_", "padspeaker_all", "padspeaker_tv_") + LEGACY_PREFIXES
    for name in ("padspeaker_1122", "padspeaker_all", "padspeaker_tv_1122", "padspeaker_tv_all"):
        assert is_ours(name)
    assert not is_ours(TV) and not is_ours("padselftest_x")
    assert is_ours("pad_1234", "pad_") and is_ours("padspeaker_tv_all", "pad_")
    assert not is_ours("padspeaker_1122", "pad_")


def test_sink_names_use_the_project_name():
    assert (PAD_PREFIX, ALL_PREFIX, TV_PREFIX) == ("padspeaker_", "padspeaker_all", "padspeaker_tv_")
    assert (COMBO_ALL, TV_ALL) == ("padspeaker_all", "padspeaker_tv_all")
    names_made = set(names(plan_combos([A, B], TV, ComboFlags())))
    assert names_made == {"padspeaker_all", "padspeaker_tv_1122", "padspeaker_tv_eeff", "padspeaker_tv_all"}
    assert all(not n.startswith("ds4") for n in names_made)


def test_sinks_of_the_pre_rename_version_are_still_recognised_as_ours():
    """The old release (ds4-bt-speaker) named its sinks ds4_*; a stale one must be cleaned up, never used as the TV."""
    assert LEGACY_PREFIXES == ("ds4_speaker_", "ds4_speakers_", "ds4_tv_")
    for name in ("ds4_speaker_1122", "ds4_speakers_all", "ds4_tv_1122", "ds4_tv_all"):
        assert is_ours(name), name
        assert is_ours(name, "custom_"), name              # whatever the configured pad prefix is
    assert plan_combos([A, B], "ds4_speaker_1122", ComboFlags()) == plan_combos([A, B], None, ComboFlags())
    with pytest.raises(ValueError):
        validate_plan([], [A, B], "ds4_tv_all")


@pytest.mark.parametrize("count", [0, 1, 2, 3, 4])
@pytest.mark.parametrize("tv", [None, TV])
@pytest.mark.parametrize("flags", [ComboFlags(*bits) for bits in itertools.product([True, False], repeat=3)])
def test_no_feedback_loops_in_any_plan(count, tv, flags):
    pads = [PadSink("00:00:00:00:00:%02x" % i, "Pad", "padspeaker_00%02x" % i) for i in range(count)]
    specs = plan_combos(pads, tv, flags)
    combo_names = set(names(specs))
    pad_sinks = {p.sink for p in pads}
    for spec in specs:
        assert not set(spec.slaves) & combo_names            # a combo never feeds a combo
        assert set(spec.slaves) <= pad_sinks | ({tv} if tv else set())
        assert spec.name not in spec.slaves
        assert len(set(spec.slaves)) == len(spec.slaves)
    validate_plan(specs, pads, tv)                           # and the validator agrees


def test_validate_plan_rejects_loops():
    good = plan_combos([A, B], TV, ComboFlags())
    validate_plan(good, [A, B], TV)
    with pytest.raises(ValueError, match="combined"):
        validate_plan([ComboSpec("padspeaker_tv_all", "d", (COMBO_ALL, A.sink), "tv_all"),
                       ComboSpec(COMBO_ALL, "d", (A.sink, B.sink), "pads")], [A, B], TV)
    with pytest.raises(ValueError, match="itself"):
        validate_plan([ComboSpec(COMBO_ALL, "d", (COMBO_ALL, A.sink), "pads")], [A, B], TV)
    with pytest.raises(ValueError, match="unknown"):
        validate_plan([ComboSpec(COMBO_ALL, "d", (A.sink, "mystery"), "pads")], [A, B], TV)
    with pytest.raises(ValueError, match="TV"):
        validate_plan([], [A], "padspeaker_tv_1122")                # the TV sink must not be one of ours


def test_plan_ignores_a_tv_sink_that_is_one_of_ours():
    assert plan_combos([A], "padspeaker_all", ComboFlags()) == []
    assert plan_combos([A], A.sink, ComboFlags()) == []


# --- labels -------------------------------------------------------------------------------

def test_the_label_goes_into_the_descriptions_but_never_into_the_names():
    label = "HD-Audio Generic Digital Stereo (HDMI 2)"
    specs = {s.name: s for s in plan_combos([A, B], TV, ComboFlags(), tv_label=label)}
    assert set(specs) == {"padspeaker_all", "padspeaker_tv_1122", "padspeaker_tv_eeff", "padspeaker_tv_all"}
    assert specs["padspeaker_tv_1122"].description == label + " + controller speaker (Wireless Controller 1122)"
    assert specs["padspeaker_tv_all"].description == label + " + all controller speakers"
    assert specs["padspeaker_all"].description == "Controller speakers (all)"
    assert all(label not in n for n in specs)


def test_default_label_stays_tv():
    (spec,) = plan_combos([A], TV, ComboFlags())
    assert spec.description == "TV + controller speaker (Wireless Controller 1122)"


def test_sanitize_label_strips_control_characters_and_quotes():
    assert sanitize_label('Sound "Bar"\n\t(1\x00)') == "Sound Bar (1)"
    assert sanitize_label("It's a \\ test") == "Its a test"
    assert sanitize_label("  many    spaces  ") == "many spaces"
    assert sanitize_label("Ünïcode ok") == "Ünïcode ok"


def test_sanitize_label_caps_the_length():
    out = sanitize_label("x" * 200)
    assert len(out) == 60
    assert sanitize_label("word " * 30) == ("word " * 12).strip()[:60].rstrip()
    assert len(sanitize_label("word " * 30)) <= 60


@pytest.mark.parametrize("bad", ["", "   ", '"\'', "\n\x01", None])
def test_sanitize_label_empty_results_are_none(bad):
    assert sanitize_label(bad) is None


def test_resolve_label_priority():
    descriptions = {TV: "HD-Audio Generic (HDMI 2)", "other": ""}
    assert resolve_label("TV", TV, descriptions) == "TV"                 # configured wins
    assert resolve_label(None, TV, descriptions) == "HD-Audio Generic (HDMI 2)"
    assert resolve_label("   ", TV, descriptions) == "HD-Audio Generic (HDMI 2)"
    assert resolve_label(None, "other", descriptions) == "other"         # no description: the name
    assert resolve_label(None, "unknown", descriptions) == "unknown"
    assert resolve_label(None, None, descriptions) == "TV"               # nothing known


# --- labels from the real output (EDID name) ------------------------------------------------

from tests.test_sinks import PACTL_LIST_SINKS                      # noqa: E402
from padspeaker.sinks import parse_sink_details                    # noqa: E402

SAMPLE = parse_sink_details(PACTL_LIST_SINKS)
EXAMPLE_DISPLAY = "alsa_output.pci-0000_00_1f.3.hdmi-stereo-extra1"
GENERIC = "alsa_output.pci-0000_00_1f.3.analog-stereo"
USB = "alsa_output.usb-Focusrite_Scarlett_2i2-00.analog-stereo"


def label_for(name, configured=None):
    descriptions = {n: d.description for n, d in SAMPLE.items()}
    return resolve_label(configured, name, descriptions, {n: d.properties for n, d in SAMPLE.items()})


def test_label_prefers_the_tvs_own_name_with_the_short_port():
    assert label_for(EXAMPLE_DISPLAY) == "Example Display (HDMI 2)"


def test_label_with_the_example_display_sample_reaches_the_descriptions():
    specs = {s.name: s for s in plan_combos([A, B], EXAMPLE_DISPLAY, ComboFlags(),
                                            tv_label=label_for(EXAMPLE_DISPLAY))}
    assert specs["padspeaker_tv_1122"].description == \
        "Example Display (HDMI 2) + controller speaker (Wireless Controller 1122)"
    assert specs["padspeaker_tv_all"].description == "Example Display (HDMI 2) + all controller speakers"


def test_configured_label_beats_the_tvs_name():
    assert label_for(EXAMPLE_DISPLAY, configured="TV") == "TV"


def test_generic_nick_falls_back_to_the_description():
    # node.nick "HDMI" says nothing about the device
    assert derive_label("HD-Audio Generic Analog Stereo",
                        {"node.nick": "HDMI", "device.description": "HD-Audio Generic"}) \
        == "HD-Audio Generic Analog Stereo"


def test_alsa_name_is_tried_when_the_nick_is_generic():
    assert derive_label("HD-Audio Generic Digital Stereo (HDMI 2)",
                        {"node.nick": "HDMI", "alsa.name": "DELL U2720Q",
                         "device.description": "HD-Audio Generic"}) == "DELL U2720Q (HDMI 2)"


@pytest.mark.parametrize("nick", ["HDMI", "hdmi", "default", "Default", "Analog", "analog", "",
                                  "  ", "HD-Audio Generic", "hd-audio generic", "Speaker",
                                  "Headphones", "Digital", "SPDIF"])
def test_generic_values_are_never_used_as_a_name(nick):
    label = derive_label("Some Card Digital Stereo (HDMI 3)",
                         {"node.nick": nick, "device.description": "HD-Audio Generic"})
    assert label == "Some Card Digital Stereo (HDMI 3)"


def test_nick_equal_to_the_card_name_is_generic():
    assert derive_label("Card Analog Stereo", {"node.nick": "My Card", "alsa.card_name": "My Card"}) \
        == "Card Analog Stereo"
    assert derive_label("Card Analog Stereo", {"node.nick": "My Card",
                                               "device.description": "my card"}) == "Card Analog Stereo"


def test_usb_dac_nick_is_used_without_a_port():
    assert label_for(USB) == "USB Audio"


def test_sink_without_properties_uses_its_description():
    assert label_for("bare_sink") == "A sink without properties"


def test_nick_with_its_own_parentheses_gets_no_second_port():
    assert derive_label("X Digital Stereo (HDMI 2)", {"node.nick": "TV (living room)"}) == \
        "TV (living room)"


def test_nick_is_cleaned_like_any_label():
    label = derive_label("X (HDMI)", {"node.nick": 'LG "OLED" \n55'})
    assert label == "LG OLED 55 (HDMI)"
    assert len(derive_label("X (HDMI 1)", {"node.nick": "N" * 100})) <= 60


def test_port_is_the_last_parenthesised_group_of_the_description():
    assert derive_label("Foo (Bar) Digital Stereo (HDMI 2)", {"node.nick": "TV7"}) == "TV7 (HDMI 2)"
    assert derive_label("Foo Digital Stereo", {"node.nick": "TV7"}) == "TV7"


def test_unknown_sink_and_empty_inputs():
    assert label_for("nope") == "nope"
    assert resolve_label(None, None, {}, {}) == "TV"
    assert resolve_label(None, "x", {}, None) == "x"


def test_resolved_label_is_safe_for_pactl():
    from padspeaker.sinks import quote_sink_properties
    label = resolve_label(None, TV, {TV: 'Bad "name" \x07 with \'quotes\''})
    quote_sink_properties(label + " + controller speaker (x)")           # must not raise
    assert '"' not in label and "'" not in label


# --- manager ----------------------------------------------------------------------------

class FakeSinks:
    def __init__(self):
        self.events = []
        self.default = TV
        self.modules = {}          # sink name -> module id
        self.slaves = {}
        self.next_id = 200
        self.fail_create = 0
        self.set_default_fails = False

    def create_combine_sink(self, name, description, slaves):
        if self.fail_create:
            self.fail_create -= 1
            raise SinkError("module failed")
        self.next_id += 1
        self.modules[name] = self.next_id
        self.slaves[name] = tuple(slaves)
        self.events.append(("create", name, tuple(slaves)))
        return self.next_id

    def remove_null_sink(self, module):
        name = next(n for n, m in self.modules.items() if m == module or n == module)
        del self.modules[name]
        self.events.append(("remove", name))

    def default_sink(self):
        return self.default

    def set_default_sink(self, name):
        if self.set_default_fails:
            raise SinkError("no")
        self.default = name
        self.events.append(("set_default", name))

    def kinds(self, kind):
        return [e for e in self.events if e[0] == kind]


class Harness:
    def __init__(self, flags=None, previous=TV):
        self.sinks = FakeSinks()
        self.fallbacks = []
        self.previous = previous
        self.manager = ComboManager(self.sinks, flags or ComboFlags(), fallback=self._fallback,
                                    previous_default=lambda: self.previous)

    def _fallback(self, previous):
        self.fallbacks.append(previous)
        self.sinks.events.append(("fallback", previous))
        self.sinks.default = previous

    def update(self, pads, tv=TV, extra_present=(), label=None):
        present = [p.sink for p in pads] + ([tv] if tv else []) + list(self.sinks.modules) \
            + list(extra_present)
        if label is None:
            return self.manager.update(pads, tv, present)
        return self.manager.update(pads, tv, present, tv_label=label)


def test_creates_the_planned_combos():
    h = Harness()
    assert h.update([A, B]) is True
    assert {e[1] for e in h.sinks.kinds("create")} == {"padspeaker_all", "padspeaker_tv_1122",
                                                      "padspeaker_tv_eeff", "padspeaker_tv_all"}
    assert h.manager.slaves_of()["padspeaker_tv_all"] == (TV, A.sink, B.sink)


def test_nothing_is_rebuilt_when_nothing_changed():
    h = Harness()
    h.update([A, B])
    h.sinks.events.clear()
    assert h.update([A, B]) is True
    assert h.sinks.events == []


def test_pad_leaving_removes_only_the_combos_that_involved_it():
    h = Harness()
    h.update([A, B])
    h.sinks.events.clear()
    h.update([A])
    assert {e[1] for e in h.sinks.kinds("remove")} == {"padspeaker_all", "padspeaker_tv_all", "padspeaker_tv_eeff"}
    assert h.sinks.kinds("create") == []
    assert set(h.manager.slaves_of()) == {"padspeaker_tv_1122"}


def test_pad_joining_creates_and_rebuilds_the_combos_that_gain_a_slave():
    h = Harness()
    h.update([A])
    h.sinks.events.clear()
    h.update([A, B])
    assert {e[1] for e in h.sinks.kinds("create")} == {"padspeaker_all", "padspeaker_tv_eeff", "padspeaker_tv_all"}
    assert h.sinks.kinds("remove") == []                 # padspeaker_tv_1122 is unchanged
    h.sinks.events.clear()
    h.update([A, B, C])
    removed = {e[1] for e in h.sinks.kinds("remove")}
    assert removed == {"padspeaker_all", "padspeaker_tv_all"}      # their slave lists grew
    assert {e[1] for e in h.sinks.kinds("create")} == {"padspeaker_all", "padspeaker_tv_all", "padspeaker_tv_5566"}
    assert h.sinks.slaves["padspeaker_all"] == (C.sink, A.sink, B.sink)         # ordered by address


def test_tv_change_rebuilds_the_tv_combos_only():
    h = Harness()
    h.update([A, B])
    h.sinks.events.clear()
    h.update([A, B], tv="alsa_output.analog")
    assert {e[1] for e in h.sinks.kinds("remove")} == {"padspeaker_tv_1122", "padspeaker_tv_eeff", "padspeaker_tv_all"}
    assert h.sinks.slaves["padspeaker_tv_1122"] == ("alsa_output.analog", A.sink)
    h.sinks.events.clear()
    h.update([A, B], tv=None)                            # TV went away: TV combos disappear
    assert {e[1] for e in h.sinks.kinds("remove")} == {"padspeaker_tv_1122", "padspeaker_tv_eeff", "padspeaker_tv_all"}
    assert set(h.manager.slaves_of()) == {"padspeaker_all"}


def test_changed_label_rebuilds_only_the_tv_combos_and_keeps_the_default():
    h = Harness()
    h.update([A, B], label="HDMI")
    h.sinks.default = "padspeaker_tv_all"
    h.sinks.events.clear()
    h.update([A, B], label="TV")
    assert {e[1] for e in h.sinks.kinds("remove")} == {"padspeaker_tv_1122", "padspeaker_tv_eeff", "padspeaker_tv_all"}
    assert {e[1] for e in h.sinks.kinds("create")} == {"padspeaker_tv_1122", "padspeaker_tv_eeff", "padspeaker_tv_all"}
    assert h.sinks.default == "padspeaker_tv_all" and h.fallbacks == []
    h.sinks.events.clear()
    h.update([A, B], label="TV")
    assert h.sinks.events == []                              # same label: nothing to do


def test_snapshot_carries_the_label_in_the_description():
    h = Harness()
    h.update([A], label="Sound bar")
    assert h.manager.snapshot()[0]["description"] == "Sound bar + controller speaker (Wireless Controller 1122)"


def test_disabled_groups_are_never_created():
    h = Harness(ComboFlags(pads=False, tv_pad=False, tv_all=True))
    h.update([A, B])
    assert {e[1] for e in h.sinks.kinds("create")} == {TV_ALL}


def test_default_combo_that_disappears_falls_back_to_the_previous_default_first():
    h = Harness(previous="alsa_output.analog")
    h.update([A, B])
    h.sinks.default = "padspeaker_all"
    h.sinks.events.clear()
    h.update([A])                                        # padspeaker_all is no longer planned
    assert h.fallbacks == ["alsa_output.analog"]
    events = [e[0] + ":" + e[1] for e in h.sinks.events if e[0] in ("fallback", "remove")
              if e[1] is not None]
    assert events.index("fallback:alsa_output.analog") < events.index("remove:padspeaker_all")


def test_no_fallback_when_the_default_is_something_else():
    h = Harness()
    h.update([A, B])
    h.update([A])
    assert h.fallbacks == []


def test_rebuilding_the_default_combo_keeps_it_the_default():
    h = Harness()
    h.update([A, B])
    h.sinks.default = "padspeaker_all"
    h.update([A, B, C])                                  # same name, longer slave list
    assert h.fallbacks == []
    assert h.sinks.default == "padspeaker_all"
    assert ("set_default", "padspeaker_all") in h.sinks.events


def test_combo_with_a_missing_slave_is_postponed_not_created():
    h = Harness()
    present_missing = h.manager.update([A, B], TV, [A.sink, TV])          # B's sink does not exist yet
    assert present_missing is False
    assert {e[1] for e in h.sinks.kinds("create")} == {"padspeaker_tv_1122"}
    assert h.update([A, B]) is True
    assert "padspeaker_all" in h.sinks.modules


def test_stale_combo_from_an_earlier_run_is_replaced():
    h = Harness()
    h.sinks.modules["padspeaker_all"] = 5                  # exists, unknown slave list
    h.sinks.default = "padspeaker_all"
    h.manager.update([A, B], TV, [A.sink, B.sink, TV, "padspeaker_all"])
    assert h.sinks.kinds("remove")[0] == ("remove", "padspeaker_all")
    assert h.sinks.slaves["padspeaker_all"] == (A.sink, B.sink)
    assert h.sinks.default == "padspeaker_all"             # and it stays the default
    assert h.fallbacks == []


def test_combo_that_vanished_on_its_own_is_recreated():
    h = Harness()
    h.update([A])
    del h.sinks.modules["padspeaker_tv_1122"]                       # e.g. the audio server restarted
    h.sinks.events.clear()
    assert h.manager.update([A], TV, [A.sink, TV]) is True
    assert h.sinks.kinds("create") == [("create", "padspeaker_tv_1122", (TV, A.sink))]


def test_create_failure_is_reported_and_retried_without_raising():
    h = Harness()
    h.sinks.fail_create = 1
    assert h.update([A]) is False
    assert h.manager.slaves_of() == {}
    assert h.update([A]) is True
    assert "padspeaker_tv_1122" in h.manager.slaves_of()


def test_a_tv_sink_that_is_one_of_ours_is_ignored():
    h = Harness()
    h.update([A, B], tv="padspeaker_all")
    assert {e[1] for e in h.sinks.kinds("create")} == {"padspeaker_all"}


def test_remove_all_restores_the_default_when_it_was_a_combo():
    h = Harness(previous="alsa_output.analog")
    h.update([A, B])
    h.sinks.default = "padspeaker_tv_1122"
    h.manager.remove_all()
    assert h.fallbacks == ["alsa_output.analog"]
    assert h.sinks.modules == {} and h.manager.slaves_of() == {}
    h.manager.remove_all()                                  # idempotent


def test_remove_errors_are_contained():
    h = Harness()
    h.update([A, B])

    def boom(module):
        raise SinkError("unload failed")
    h.sinks.remove_null_sink = boom
    h.manager.remove_all()                                  # no exception


def test_snapshot_lists_combos():
    h = Harness()
    h.update([A])
    (item,) = h.manager.snapshot()
    assert item == {"name": "padspeaker_tv_1122", "description": "TV + controller speaker (Wireless Controller 1122)",
                    "slaves": [TV, A.sink], "group": "tv_pad"}
