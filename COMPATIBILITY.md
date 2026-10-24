# Compatibility

What has been tried, on what, and what has not. "Verified" means somebody ran it on that hardware and heard the sound;
"tests only" means the behaviour is covered by the automated tests (with fakes) but nobody has recorded running it on
hardware. If you can move something from one list to another, please [report your pad](#report-your-pad).

## Verified

| | |
|---|---|
| Pads | two DS4-compatible clones that identify themselves as Sony `054C:09CC` (DualShock 4 v2) with the HID name `Wireless Controller` |
| Connection | Bluetooth, normal PlayStation mode, bound by the kernel's `hid-playstation` driver |
| System | Batocera v41, kernel 6.11, PipeWire 1.2 with `pipewire-pulse`, BlueZ, Python 3.11 |
| What works | a test tone, and sound from the daemon's `Controller speaker` output, on the pad's speaker; speaker volume following the output's volume; the lightbar kept on while streaming and restored afterwards; no more drop-outs after about 14 seconds once sniff mode is turned off (that failure happened every time without the fix); the combined outputs (all pads, main output plus one pad, main output plus all pads), both pads streaming at the same time, and the output labels taken from the display's own name; installation with `deploy/install.sh` and running as an enabled Batocera service |

On these clones audio reports alone switched the lightbar off, which is why the light colour is re-sent while streaming
(`keep_lightbar`). A genuine Sony pad may behave differently.

## Tests only (not recorded on hardware)

- Starting at boot: the service is installed and enabled on the verified system, but a reboot with the pads connecting afterwards
  has not been recorded yet.
- Python 3.9, 3.10, 3.12 and 3.13 (3.11 is what Batocera ships and what the verified system runs; the test suite runs on 3.9 to
  3.13 in CI).
- The optional `tv_delay_ms` is accepted but not implemented.

## Not verified

- **Genuine Sony DualShock 4 v1 (`054C:05C4`) and v2 (`054C:09CC`) pads.** The protocol is the one documented for them (see
  [docs/PROTOCOL.md](docs/PROTOCOL.md)), so they should work, but nobody has reported it yet.
- **The Sony DualShock 4 USB wireless adapter (`054C:0BA0`)**: its id is in the list, but it is a different path and has not been tried.
- **Other clones.** Many report Sony ids and work, others report their own ids or ignore the speaker data. A clone with other ids
  can be tried with `extra_pad_ids` (see the README); if it accepts the DS4 speaker reports it will play.
- **Other Linux distributions and desktops**, other Bluetooth adapters and BlueZ versions, PulseAudio (as opposed to PipeWire),
  `hid-sony` (as opposed to `hid-playstation`), kernels other than 6.11. [docs/LINUX.md](docs/LINUX.md) has the setup; the
  systemd units and the udev rule are untested examples.
- Latency and stutter with Wi-Fi traffic on adapters that share the 2.4 GHz band.

## Not supported

- **DualSense (PS5).** A different protocol. Not planned.
- **Pads connected by USB.** A USB DualShock 4 is already a sound card; this project does not touch it.
- **The headset jack and the microphone.** Only the built-in speaker is used (the report format has a headset target, it is not used).
- Non-Linux systems.

## Report your pad

Whether it works or not, a report helps the next person. Open a GitHub issue with the **Pad report** form (or copy this
into an issue). Everything below is read-only except `tone`, which plays a sound on the pad.

1. Versions and system:

   ```sh
   padspeaker --version          # or: python3 -m padspeaker --version
   uname -r
   bluetoothctl --version
   pactl info | grep -E 'Server (Name|String)'
   python3 --version
   ```

2. What the program sees (paste the whole output):

   ```sh
   padspeaker list --json
   padspeaker check
   ```

   `check` also lists Bluetooth gamepads that were **not** recognised, with the `VVVV:PPPP` id to try in `extra_pad_ids`.

3. The ids of your Bluetooth HID devices, if `list` shows no pad:

   ```sh
   grep -H -E 'HID_ID|HID_NAME' /sys/class/hidraw/hidraw*/device/uevent
   ```

4. The tone (stop streaming to the pad first by selecting another output):

   ```sh
   padspeaker tone --seconds 3
   ```

   Tell us what you heard: nothing, a clear tone, crackling, a tone that stops, the pad disconnecting, the light going off.

5. For a problem while the daemon runs: the last 50 lines of its log (journal or `/userdata/system/logs/padspeaker.log`) and
   `padspeaker status`.

**What to paste and what to hide.** Bluetooth addresses (`aa:bb:cc:dd:ee:ff`) identify your devices: replace all but the first
three bytes with `xx` (`aa:bb:cc:xx:xx:xx`). The vendor and product ids (`054C:09CC`), the HID name, the kernel version and
the audio server name are what we need; host names, user names and addresses in paths are not.
