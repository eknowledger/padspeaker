# padspeaker on Batocera

Written for Batocera v41 (PipeWire with the pulse layer, Python 3.11, kernel 6.11 with `hid-playstation`). Everything
lives under `/userdata`, which survives reboots and updates. Nothing in `/usr` (read-only on Batocera) is touched.

## Before you start

- SSH to the Batocera works from your computer (`ssh root@batocera.local`). The installer uses key-based or saved
  passwordless login (it runs ssh with `BatchMode`, so it never stops to ask for a password; set up a key with
  `ssh-copy-id root@batocera.local` if needed).
- The pad is paired and connected **over Bluetooth in its normal PlayStation mode** (see the README, "Pairing modes").
- At least 50 MB free on `/userdata`. The app itself is well under 1 MB.

The installer checks these on the Batocera before it changes anything: `python3` (3.9 or newer), `libsbc.so.1`,
`pactl`, `parec`, free space. Batocera v41 has all of them. If something is missing it prints every problem with its fix
and stops.

## Install

Run on your computer, from a checkout of this repository:

```sh
deploy/install.sh --dry-run          # shows exactly what would be sent and run; changes nothing
deploy/install.sh                    # installs, but does NOT enable the service
```

Use `--host user@address` (or put the address last) if `root@batocera.local` does not resolve, for example
`deploy/install.sh --host root@192.168.1.100`.

What the installer does, in order, and why it is safe to run again at any time:

1. builds a tarball of `padspeaker/`, `tools/`, the service script, `VERSION` and a `MANIFEST.sha256`;
2. checks the Batocera (read-only);
3. uploads the tarball into a fresh `.staging-...` folder inside the install folder, compares its sha256, unpacks it,
   verifies every file against the manifest, compiles every `.py` with the box's Python and runs
   `python3 -m padspeaker --version` on the staged copy;
4. only then swaps the new version in by renames. The previous version stays next to it as `padspeaker.previous/`,
   `tools.previous/` and `service.previous/`; a failure during the swap puts the old one back;
5. verifies the installed files again and cleans up the staging folder;
6. if it finds an install made under the project's earlier name (`ds4-bt-speaker`), migrates it, see
   [Coming from the old name](#coming-from-the-old-name).

If the service is already running when you update, it is restarted so that it runs the new code. Otherwise nothing is
started.

## Try it before enabling it

```sh
ssh root@batocera.local 'cd /userdata/system/padspeaker && python3 tools/box_selftest.py'
ssh root@batocera.local 'cd /userdata/system/padspeaker && python3 tools/box_selftest.py --tone'
```

The first prints PASS, FAIL or SKIP for libsbc, an encoded frame, `pactl` and `parec`, the audio server, a temporary test
sink (created and removed again) and a recording from its monitor, pad discovery, write access to the pad's
`/dev/hidrawN` (opened only) and the lightbar LED class. The second plays a 3 second tone on the first pad and puts the
lightbar colour back. Without a connected pad the pad checks are skipped.

## Enable it

The installer prints this exact command at the end of an install. It is the one thing that changes
`/userdata/system/batocera.conf` (it adds `padspeaker` to the `system.services` line):

```sh
ssh root@batocera.local 'batocera-services enable padspeaker && batocera-services start padspeaker'
```

Or install and enable in one go with `deploy/install.sh --enable`. On the Batocera itself you can also switch it on
under Start > System Settings > Services (the entry is called `padspeaker`).

Check it:

```sh
ssh root@batocera.local batocera-services status padspeaker       # started / stopped
ssh root@batocera.local tail -n 30 /userdata/system/logs/padspeaker.log
ssh root@batocera.local 'cd /userdata/system/padspeaker && XDG_RUNTIME_DIR=/var/run python3 -m padspeaker list'
ssh root@batocera.local 'cd /userdata/system/padspeaker && XDG_RUNTIME_DIR=/var/run python3 -m padspeaker status'
```

## Pick the controller speaker as the output

With the pad connected and the service running, a new output appears, one per pad, called
`Controller speaker (Wireless Controller xxxx)` (`xxxx` is the end of the pad's Bluetooth address; the sink behind it is
`padspeaker_xxxx`). Choose it on the Batocera:

**Start > System Settings > Audio Output** (the system volume keys change its volume too).

While it is the selected output the daemon streams to the pad and re-sends the light colour about twice a second (the
light would otherwise go off, see `keep_lightbar` in the README); when you select another output it stops and puts the
lightbar back. The daemon never changes your default output by itself, except to move it back to the previous output if
the pad disconnects while it is selected, so the sound does not go silent. On the Batocera it also resets the saved
`audio.device` setting (the value behind this menu) to that output with `batocera-settings-set`, but only if the saved
value was one of the controller speaker outputs, so the menu does not show a raw name such as `padspeaker_1122` for an
output that is gone.

When the pad disconnects, its entry disappears from the menu.

### Controller speaker and your TV (main output) together

The same menu also lists **combined outputs**, created by the daemon: `Controller speakers (all)` (with two or more pads),
`<output> + controller speaker (Wireless Controller xxxx)` (one per pad) and `<output> + all controller speakers` (with two
or more pads). `<output>` is the name of the output that was selected when the first pad appeared, or of the one set
with `tv_sink`. It is the TV's own name when the TV reports one (from the HDMI EDID: `Example Display (HDMI 2)`), found in the
sink properties `node.nick` or `alsa.name` (`pactl list sinks` shows them; generic values such as `HDMI` or the card
name `HD-Audio Generic` are ignored); otherwise the output's description
(`HD-Audio Generic Digital Stereo (HDMI 2)`). To read `TV` instead, set `"tv_label": "TV"` in `config.json`, which takes
priority over everything. The sink names stay `padspeaker_tv_xxxx` and `padspeaker_tv_all` whatever the wording. Choose one of them and the sound plays on those speakers at the same time; the system volume keys change the
combined output, and the pad speakers follow it. When a pad is switched off the combinations that included it disappear
and, if one of them was selected, the daemon selects your previous output again and resets the saved `audio.device`.

Things to know:
- The main output (the TV) is **100 to 150 ms ahead** of the pad speaker (no delay compensation yet, `tv_delay_ms` is only planned).
- Each pad starts streaming when its own output is selected **or** sound is flowing into it through a combined output,
  and stops 2 seconds after neither is true. If a pad keeps streaming after you went back to the TV, try
  `start_on_running: false` and report it (see the README).
- Switch groups off with `combine_pads`, `combine_tv_pad`, `combine_tv_all`; `padspeaker list` shows what exists.

## Where things live

| path | what |
|---|---|
| `/userdata/system/padspeaker/padspeaker/` | the Python package |
| `/userdata/system/padspeaker/tools/` | `box_selftest.py`, `ds4_tone_poc.py` |
| `/userdata/system/padspeaker/service/padspeaker` | the packaged copy of the service script |
| `/userdata/system/padspeaker/VERSION`, `MANIFEST.sha256` | version and checksums of every installed file |
| `/userdata/system/padspeaker/*.previous/` | the version before the last update (one generation) |
| `/userdata/system/padspeaker/config.json` | your optional settings (you create it; never overwritten) |
| `/userdata/system/services/padspeaker` | the Batocera service script (start, stop, restart, status) |
| `/userdata/system/logs/padspeaker.log` (`.1` to `.3`) | the daemon's log, rotated at about 1 MB |
| `/userdata/system/logs/padspeaker.log.console` | what the daemon printed to the console in this run (a Python error at startup shows here) |
| `/var/run/padspeaker.pid` | pid file, gone after a reboot |

The service script starts `python3 -m padspeaker run --log-file ...` in the background with
`XDG_RUNTIME_DIR=/var/run` (where Batocera's PipeWire keeps its socket, the same value Batocera's own scripts use) and
`PYTHONPATH` set to the install folder, and passes `--config` when `config.json` exists. Starting it twice is harmless.
Stopping sends SIGTERM so the daemon can remove its sinks and restore the lightbar, and SIGKILL only after 10 seconds. It
only ever signals a process whose command line is ours.

To change a setting, create `/userdata/system/padspeaker/config.json` (see the README for the keys) and run
`batocera-services restart padspeaker`.

## What is NOT modified

- Nothing outside `/userdata`: no file in `/usr`, `/etc`, `/boot`.
- No file of yours or of Batocera under `/userdata` except one line: enabling the service adds `padspeaker` to
  `system.services` in `/userdata/system/batocera.conf` (done by Batocera's own `batocera-services`; disabling removes
  it). Without `--enable` and without the command above, the installer never touches `batocera.conf`. (While running,
  the daemon may ask `batocera-settings-set` to reset `audio.device`, see below.)
- No `custom.sh`, no autostart script, no change to EmulationStation, to the audio configuration or to the default
  output except moving it back when a pad vanishes (see above), and no change to the Bluetooth configuration files or to
  the pads' input. The only things written to a pad are the two HID output report types described in
  [PROTOCOL.md](PROTOCOL.md), and only while it is the selected output.
- At run time, and not persistently, the daemon does three small things through Batocera's own tools, none of which
  edits a file by hand: it removes sniff mode from a pad's Bluetooth link (`hcitool lp <MAC> RSWITCH`, reset by BlueZ
  on the next connection, see [PROTOCOL.md](PROTOCOL.md)), it runs `bluetoothctl disconnect <MAC>` if a pad's HID device
  vanishes while the link is up, and after a pad vanishes it may run `batocera-settings-set audio.device <output>`.
  Each can be switched off in the configuration (`disable_sniff`, `disconnect_on_hid_loss`,
  `sync_batocera_audio_device`).
- Also at run time the daemon loads and unloads PipeWire/PulseAudio modules with `pactl` (`module-null-sink` per pad and
  `module-combine-sink` for the combined outputs); they are removed again when a pad goes or the daemon stops.
- The installer only writes inside `/userdata/system/padspeaker/` and the single file
  `/userdata/system/services/padspeaker`, and the log files. The one exception is the migration from the old name
  described below, which renames the old install folder, removes the old service script and may edit `system.services`
  through `batocera-services`.

## Update

Run `deploy/install.sh` again (add `--enable` if you want it enabled in the same go). It keeps `config.json`, the logs and
one previous version.

To go back by hand:

```sh
ssh root@batocera.local 'cd /userdata/system/padspeaker && for n in padspeaker tools service; do rm -rf $n.bad; mv $n $n.bad && mv $n.previous $n; done && cp service/padspeaker /userdata/system/services/padspeaker && batocera-services restart padspeaker'
```

## Coming from the old name

The project was called `ds4-bt-speaker` before its first public release (install folder
`/userdata/system/ds4-bt-speaker`, service `ds4_bt_speaker`). When `deploy/install.sh` finds that old install it
migrates it, after the new version has been installed and verified:

1. it stops the old service (and gives up, leaving the old install untouched, if the service does not stop);
2. if the old service was enabled it runs `batocera-services disable ds4_bt_speaker`, and the new `padspeaker` service is
   enabled and started instead; otherwise the new service is left as it would be on a fresh install;
3. it removes the old service script `/userdata/system/services/ds4_bt_speaker`;
4. it copies the old `config.json` to `/userdata/system/padspeaker/config.json`, but never over a `config.json` that is
   already there;
5. it renames the whole old folder to `/userdata/system/ds4-bt-speaker.old-<timestamp>`. Nothing is deleted; remove that
   backup yourself when you no longer need it.

The old log files `/userdata/system/logs/ds4-bt-speaker.log*` stay where they are; the new log is
`/userdata/system/logs/padspeaker.log`. Every other file is left alone. Run the installer again and it finds nothing
more to migrate. `--dry-run` prints the migration step without running it. If your `config.json` set `sink_name_prefix`,
it is copied as it is. The virtual outputs are now called `padspeaker_xxxx`, `padspeaker_all` and `padspeaker_tv_...`
instead of `ds4_speaker_xxxx`, `ds4_speakers_all` and `ds4_tv_...`, so choose the controller speaker once more under
Start > System Settings > Audio Output; a saved choice that still names an old output is reset by the daemon like any
other of its outputs.

## Uninstall

```sh
deploy/install.sh --uninstall --dry-run      # look first
deploy/install.sh --uninstall                # stop, disable, remove the app and the service script
deploy/install.sh --uninstall --purge --yes  # also delete config.json and the logs
```

`--uninstall` stops the service, runs `batocera-services disable padspeaker`, removes the app folders (and their
`.previous` copies) and the service script. It keeps `config.json`, any other file you put in the install folder, and
the logs; if nothing is left the folder is removed as well. `--purge --yes` removes the install folder and the log
files too. Nothing else on the Batocera is touched; in particular a `ds4-bt-speaker.old-<timestamp>` backup folder from
the migration is never removed by the installer.

## Notes for Batocera updates

A Batocera upgrade replaces `/usr` but keeps `/userdata`, so the install survives it. If an upgrade ever changes the
Python version or removes `libsbc.so.1` or `pactl`, run `python3 tools/box_selftest.py` on the box: it names what is
missing.
