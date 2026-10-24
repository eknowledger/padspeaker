# Contributing

Thanks for helping. This is a small project; the rules are short. Before changing code, read
[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md): how it is put together, why, and which test guards what.

## Set up and test

```sh
git clone https://github.com/eknowledger/padspeaker.git
cd padspeaker
python3 -m venv .venv
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m pytest
```

The tests need no hardware, no PipeWire and no libsbc (the one SBC encoder test is skipped without it). CI runs them on
Python 3.9 and newer (CI runs 3.9, 3.11 and 3.13), so keep the code compatible with 3.9: no `match`, no `X | Y` types at runtime, no new syntax.

## Ground rules

- **Test first.** A bug fix starts with a failing test; a feature comes with tests. The package is built as a pure core
  (`protocol`, `pacer`, the streamer's state machine) plus thin adapters (`sbc`, `sinks`, `capture`, `pads`, `leds`) with
  injectable I/O, so almost everything can be tested with fakes. Keep it that way.
- **No runtime dependencies.** Standard library, `libsbc` through `ctypes`, and the `pactl` / `parec` programs.
- **Be careful with the pad.** Only output reports `0x11` and `0x17` are ever written, only to recognised Bluetooth pads (the built-in
  Sony DS4 ids, or ids the user added in `extra_pad_ids`), with the volume capped. Do not add writes of anything else without discussing it first, and never touch the
  pad's input path.
- **Be careful with the box.** `deploy/install.sh` must stay inside `/userdata/system/padspeaker`, the one service
  script and the log files (plus, once, the move of an install made under the old name `ds4-bt-speaker`; see
  `docs/BATOCERA.md`). `tests/test_deploy.py` runs the real remote scripts against a fake `ssh` and a sandbox and
  checks that no other file changes; extend it when you add a step. Never run the installer against a real machine in a
  test.
- **Docs move with the code.** If you change a protocol detail, a config key or an installer option, update
  `docs/PROTOCOL.md`, the README and `docs/BATOCERA.md` in the same change. A test checks that every config key is
  documented.
- **No personal data.** Use `aa:bb:cc:dd:ee:ff` for Bluetooth addresses, `Example Display` or `Living Room TV` for display names and
  `192.168.1.100` for addresses. A test fails if a real-looking address or a known personal string appears anywhere in the repository.
- Keep messages for users in plain language and say how to fix the problem.

## Testing on hardware

`tools/ds4_tone_poc.py` and `python3 -m padspeaker tone` play a tone on a connected pad;
`tools/box_selftest.py [--tone]` checks a whole machine. If you test a pad model that works or does not work, please
open an issue with the "Bug report / pad report" form (the steps are in [COMPATIBILITY.md](COMPATIBILITY.md)), so the lists there can be updated.

## Commits and pull requests

Small commits with a short imperative subject (`feat: ...`, `fix: ...`, `docs: ...`, `test: ...`) and the reason in the
body when it is not obvious. Describe how you tested the change, and on what hardware if it touched the pad.

By contributing you agree that your work is released under the MIT license of this project.
