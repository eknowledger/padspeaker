# Security

## Reporting a problem

Please do not open a public issue for a problem that could be exploited. Report it privately through GitHub's
private vulnerability reporting: <https://github.com/eknowledger/padspeaker/security/advisories/new>.

Include what you did, what you expected and what happened. We will acknowledge a report and say what we plan to do.

## Supported versions

Only the latest release is supported (the project is at version 0.1.0).

## What the program does on your machine

padspeaker is a local program. It opens no network port and talks to no server. The installer for Batocera
(`deploy/install.sh`) is the only part that uses the network, and it only runs `ssh` to the machine you name.

It reads: sysfs (`/sys/class/hidraw`, `/sys/class/leds`, input capabilities) to find pads, `/proc` (the service script, to
check that a pid is its own), the optional JSON configuration file, and the audio server through `pactl` and `parec`.

**It writes only the following.**

| what | where | why | how to switch it off |
|---|---|---|---|
| HID output reports `0x17` (audio) and `0x11` (volume, lightbar colour) | `/dev/hidrawN` of **recognised** pads only: Bluetooth bus, the built-in Sony DS4 ids, plus any ids you added in `extra_pad_ids` | the point of the program | `ignore_macs`, `only_macs`; do not add `extra_pad_ids`; stop the service |
| Bluetooth link policy: `hcitool lp <address> RSWITCH` | the pad's link, through BlueZ | stops the link going into sniff mode, which makes the pad drop out | `disable_sniff: false` |
| `bluetoothctl disconnect <address>` | the pad's link | cleanly disconnects a pad whose HID device vanished (at most once a minute per pad) | `disconnect_on_hid_loss: false` |
| lightbar brightness files `/sys/class/leds/<pad>:red\|green\|blue/brightness` | the kernel LED class of the pad | restores the light colour after streaming (the values it read before) | cannot be switched off separately (it only puts back what it read); stop the service |
| the audio server: virtual outputs (`module-null-sink`, `module-combine-sink`), the default output | `pactl load-module`, `unload-module`, `set-default-sink` | the outputs in your sound menu; moving the default back when a selected pad vanishes | `combine_pads`, `combine_tv_pad`, `combine_tv_all: false` for the combined ones |
| **Batocera only:** the saved `audio.device` setting | through `batocera-settings-set` (never by editing the file) | so the menu does not show a gone output | `sync_batocera_audio_device: false` |
| the daemon's own files | a lock file and a status file in a private 0700 directory (`$XDG_RUNTIME_DIR/padspeaker` or `/run/padspeaker`), the log file | single instance, `status` command, logs | `status_file: null` and `lock_file: null` |

It never writes anything else to a pad (no firmware, pairing, calibration or feature reports), never touches a pad's input,
and never writes to a device that is not on the recognised list. The speaker volume is capped at the pad's maximum (79).

**Commands are run without a shell**, as argument lists with timeouts. The pad addresses given to `hcitool` and
`bluetoothctl` are validated first (six hex pairs). The configuration file is JSON; unknown keys, wrong types and
out-of-range values are errors, not ignored. The directory for the lock and status files must be a real directory owned
by you and not writable by others, and the daemon refuses to use a shared place such as `/tmp`.

## Privileges

- **On Batocera the daemon runs as root**, because everything on Batocera does and `/dev/hidraw*` are root-only. The service
  script is installed in `/userdata/system/services` and starts the daemon at boot only if you enable it. The script only
  signals a process whose command line is its own.
- **On other Linux systems it does not need root.** Run it as your own user in your own session and give that user write
  access to the pad's hidraw node (a udev rule, see [docs/LINUX.md](docs/LINUX.md); the example is untested). Do not run it
  as root unless you must: root also gives it the power to run `hcitool` and `bluetoothctl` on any adapter.
- Giving a group write access to Bluetooth hidraw nodes lets members of that group send HID output reports to those devices.
  That is what the program needs; scope the rule to the Sony ids (the example does).

## The installer

`deploy/install.sh` runs on your computer and uses `ssh` to the Batocera. It writes only inside
`/userdata/system/padspeaker/`, the single file `/userdata/system/services/padspeaker` and the log files; the only
Batocera setting it can change is `system.services`, through `batocera-services enable`, and only with `--enable` or when
you run that command yourself. `--dry-run` prints everything it would send. Each upload is checked with sha256 and a
manifest before anything is replaced, the previous version is kept for rollback, and `--uninstall` removes it.
See [docs/BATOCERA.md](docs/BATOCERA.md) for the full list.

## Turning features off, summary

Minimal footprint configuration: pads that are not yours are never touched with `only_macs`; the Bluetooth helpers are off
with `disable_sniff` and `disconnect_on_hid_loss` set to `false`; no combined outputs with the three `combine_*` keys
set to `false`; no change to Batocera's settings with `sync_batocera_audio_device: false`. What remains is the audio
output of the pad you chose and the lightbar.
