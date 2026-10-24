# padspeaker changelog

All notable changes to padspeaker are listed here. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the
project uses [Semantic Versioning](https://semver.org/).

## [0.1.0] - 2026-10-19

First public release. padspeaker was developed under the working name `ds4-bt-speaker` (Python package `ds4speaker`);
everything now uses the name padspeaker, and `deploy/install.sh` migrates an existing Batocera install made under the old
name (see `docs/BATOCERA.md`).

### Added

- Speaker audio to DualShock 4 style pads over Bluetooth: SBC (dual channel, 32 kHz) in HID output report `0x17` every 16 ms,
  volume and lightbar in report `0x11`, with the CRC-32 the pad requires. See `docs/PROTOCOL.md`.
- A daemon (`padspeaker run`) that creates one virtual audio output per connected pad
  (`Controller speaker (Wireless Controller xxxx)`), streams to it when it is selected, and removes it when the pad goes away.
  Volume and mute follow the output; the default output is moved back if the pad vanishes while selected.
- Combined outputs: all pads together, the main output plus one pad, the main output plus all pads, with labels taken
  from the main output's own name (`tv_sink`, `tv_label`).
- Lightbar keep-alive while streaming, and restore afterwards from the kernel LED class.
- Bluetooth link housekeeping: sniff mode turned off with `hcitool` (fixes drop-outs after about 14 seconds), clean
  `bluetoothctl disconnect` when a pad's HID device vanishes.
- Batocera: the saved `audio.device` setting is kept in step when an output disappears.
- Configuration file and flags: `log_file`, `status_file`, `lock_file`, `runtime_dir`, `poll_interval`, `jitter_reports`,
  `speaker_volume_max`, `sink_name_prefix`, `only_macs`, `ignore_macs`, `extra_pad_ids`, `keep_lightbar`, `lightbar_fallback`,
  `disable_sniff`, `disconnect_on_hid_loss`, `sync_batocera_audio_device`, `tv_sink`, `tv_label`, `combine_pads`,
  `combine_tv_pad`, `combine_tv_all`, `tv_delay_ms` (planned, no effect), `start_on_running`.
- `extra_pad_ids` / `--extra-pad-id` to try DS4-compatible pads that report other Bluetooth ids, and a read-only hint in
  `check` that lists Bluetooth gamepads that were not recognised.
- Commands `run`, `list`, `tone`, `status`, `check`; `tools/box_selftest.py` as a deeper on-machine check.
- Batocera installer (`deploy/install.sh`) with dry run, checksums, atomic swap, rollback, `--enable`, `--uninstall`, and the
  Batocera service script.
- Generic Linux: systemd unit examples (system and user) and a udev rule example (untested), `docs/LINUX.md`.
- Documentation: README, `docs/PROTOCOL.md`, `docs/BATOCERA.md`, `docs/LINUX.md`, `COMPATIBILITY.md`, `SECURITY.md`, `CONTRIBUTING.md`.

### Known limitations

- `tv_delay_ms` is accepted but not implemented: the main output is 100 to 150 ms ahead of the pad speaker in combined outputs.
- Verified on two DS4-compatible clones only; see `COMPATIBILITY.md`.
- Without `hcitool` a pad may drop after about 14 seconds of streaming; see `docs/LINUX.md`.
