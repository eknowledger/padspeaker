# Roadmap

What might come next, in rough order of how much I want it. Nothing here is a promise. A small project
maintained in spare time moves when someone has a pad to test on, which is the real limit: every item
below needs hardware that a unit test cannot stand in for.

## Near

- **More pads and systems in [COMPATIBILITY.md](COMPATIBILITY.md).** The table lists two clones on Batocera
  v41 and nothing else. A genuine Sony pad, a second distro, PulseAudio without PipeWire: each one verified
  turns a "should work" into a row. This is the most useful contribution and needs no code (use the
  "Bug report / pad report" issue template, which also takes "it works").

## Maybe

- **DualSense.** Different report format and a different codec path, so it is a second backend, not a
  flag. Only worth starting with a pad in hand.
- **The headset jack.** The protocol has a second audio target (byte 5 of the audio report). Untested, and
  the pad's own volume handling for it differs.
- **Microphone.** The reverse direction. Needs a capture path and echo handling; no use case yet.

## Not planned

- **A GUI.** The outputs show up in the system's own audio menu, which is the interface.
- **Touching the pad's input.** Buttons, sticks, motion and touchpad are left to the kernel driver, and that
  is part of why this is safe to run next to a game.
- **Genuine-pad firmware tricks.** Nothing here modifies the pad.
