# padspeaker on a normal Linux machine

[Batocera](BATOCERA.md) is the platform this was built and tested on. Everything here is the same program run on any
other Linux. **The systemd units and the udev rule below are examples that have not been tested on a real
desktop**; they are short and written to be read and adapted, not copied blindly. What has been tested is
listed in [COMPATIBILITY.md](../COMPATIBILITY.md).

## Requirements

| what | notes |
|---|---|
| Python 3.9 or newer | no Python packages needed at run time |
| PipeWire with `pipewire-pulse`, or PulseAudio | the daemon creates its outputs with `pactl` and records them with `parec` |
| `pactl` and `parec` | Debian and Ubuntu: `apt install pulseaudio-utils`; Fedora: `dnf install pulseaudio-utils`; Arch: `pacman -S libpulse` |
| `libsbc` (`libsbc.so.1`) | Debian and Ubuntu: `apt install libsbc1`; Fedora: `dnf install sbc`; Arch: `pacman -S sbc` |
| BlueZ with a working Bluetooth adapter | to pair and connect the pad. The tools `hcitool` and `bluetoothctl` are used for the sniff mode fix and a clean disconnect, see [Without hcitool](#without-hcitool) |
| a kernel driver that exposes the pad as `/dev/hidrawN` | `hid-playstation` (kernel 5.12 and newer) or `hid-sony`. The lightbar restore needs the LED class devices of the driver |
| write access to that hidraw node | run as root, or use the udev rule below |

`python3 -m padspeaker check` (from a checkout) or `padspeaker check` (installed) lists what is missing.

## Pair the pad

Put the pad in pairing mode (hold PS and Share until the light bar flashes quickly), then:

```sh
bluetoothctl
[bluetooth]# scan on
[bluetooth]# pair <address>
[bluetooth]# trust <address>
[bluetooth]# connect <address>
```

It must be a Bluetooth connection in the pad's normal PlayStation mode. `padspeaker list` should then show it.

## Install

**From a git checkout, nothing to install:**

```sh
git clone https://github.com/eknowledger/padspeaker.git
cd padspeaker
python3 -m padspeaker check
python3 -m padspeaker run
```

**With pip, from the checkout:**

```sh
pip install .              # or: pip install --user .
padspeaker --version
```

**From a built wheel:**

```sh
python3 -m pip install build
python3 -m build --wheel   # writes dist/padspeaker-<version>-py3-none-any.whl
pip install dist/padspeaker-*.whl
```

The package has no dependencies, so it installs without network access once you have the wheel. Distributions that
mark the system Python as externally managed need a virtual environment (`python3 -m venv ~/.venvs/padspeaker &&
~/.venvs/padspeaker/bin/pip install .`) or `pipx install .`; the paths in the unit files below then change.

To remove it: `pip uninstall padspeaker` and delete the unit files and the udev rule you added.

## Let a normal user write to the pad

The hidraw nodes are root-only by default. `deploy/udev/70-padspeaker.rules` (**untested example**) gives the group
`padspeaker` write access to Bluetooth Sony hidraw nodes, with a commented `uaccess` alternative that gives the user at
the seat access without a group:

```sh
sudo groupadd --system padspeaker
sudo usermod -aG padspeaker "$USER"        # log out and in again
sudo cp deploy/udev/70-padspeaker.rules /etc/udev/rules.d/
sudo udevadm control --reload && sudo udevadm trigger --subsystem-match=hidraw
# disconnect and reconnect the pad, then:
ls -l /dev/hidraw*                         # the pad's node should now belong to group padspeaker, mode 0660
```

A pad that is added with `extra_pad_ids` needs a line of its own with its vendor id (see the comments in the file).
`padspeaker check` reports `hidraw access` as FAIL while the node is not writable.

## Run it by hand

```sh
padspeaker run             # in a terminal; Ctrl-C stops it and removes the outputs
padspeaker run -v          # with debug detail
padspeaker list            # pads, their outputs, recognised ids
padspeaker status          # what the running daemon is doing (exit 0 running, 3 not)
padspeaker tone            # a test tone on the pad, while the daemon is not streaming to it
```

Run it as the user whose audio session you want to play to (not through `sudo`): PipeWire and PulseAudio are per-user.

## Run it as a service

**As a user service (recommended on a desktop).** It runs in your session, finds your audio server on its own and needs
no root.

```sh
mkdir -p ~/.config/systemd/user
cp deploy/systemd/user/padspeaker.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now padspeaker
journalctl --user -u padspeaker -f
```

Edit `ExecStart=` if `padspeaker` is not in `~/.local/bin`. Use `sudo loginctl enable-linger "$USER"` to keep it running
when you are not logged in.

**As a system service (a dedicated box).** `deploy/systemd/padspeaker.service` runs as a named user (edit `User=`)
and points `XDG_RUNTIME_DIR` at that user's `/run/user/<uid>` (edit the number), because a system service has no
session of its own. That user needs the hidraw access above and a running audio server, which `loginctl enable-linger
<user>` provides. Do not run it as root against another user's audio session unless you have checked that your audio server accepts it.

```sh
sudo cp deploy/systemd/padspeaker.service /etc/systemd/system/
sudoedit /etc/systemd/system/padspeaker.service      # User= and XDG_RUNTIME_DIR=
sudo systemctl daemon-reload && sudo systemctl enable --now padspeaker
journalctl -u padspeaker -f
```

Both units stop the daemon with SIGTERM (it removes its outputs and restores the lights) and restart it if it fails. Settings
go in a JSON file: add `--config /path/to/config.json` to `ExecStart` (the keys are in the README).

## How the runtime directory is found

`pactl` and `parec` locate the audio server through the environment (`PULSE_SERVER`, or `XDG_RUNTIME_DIR` for the
socket under `pulse/`). The daemon passes its own environment on to them, with one exception: if you give a runtime
directory (`--runtime-dir DIR` or `runtime_dir` in the configuration), it sets `XDG_RUNTIME_DIR` to that for every `pactl`
and `parec` it starts. That is how the Batocera service reaches PipeWire from boot, where the variable is not set (it uses
`/var/run`). On a desktop session you do not need it.

The daemon's own lock and status files go in a private directory (mode 0700): `<runtime_dir>/padspeaker` if a runtime
directory is given, else `$XDG_RUNTIME_DIR/padspeaker` if that variable names a directory you own, else
`/run/padspeaker` when running as root. If none applies it refuses to start and tells you to pass `--runtime-dir`
or `--lock-file` and `--status-file`; it never falls back to `/tmp`.

## Pick the output in your desktop

While the daemon runs, each pad adds an output called `Controller speaker (Wireless Controller xxxx)` (`xxxx`: the end of
the pad's address), plus the combined outputs described in the README. Choose it like any other output:

- **GNOME:** Settings > Sound > Output > Output Device.
- **KDE Plasma:** System Settings > Audio (or the volume icon in the panel) > choose the output device.
- **Any desktop with PipeWire:** `wpctl status` lists the sinks with numbers; `wpctl set-default <number>`.
- **Any desktop with the pulse tools:** `pactl list short sinks`, then `pactl set-default-sink padspeaker_xxxx`.
- **`pavucontrol`:** the Playback tab moves one program, the Output Devices tab sets the fallback.

The exact menu wording differs between desktop versions. Some programs remember the output they were started with;
restart them or move their stream in `pavucontrol` after selecting the pad.

## Without hcitool

With audio flowing, some pads' Bluetooth links sit in sniff mode and BlueZ then fails to send a HID message in time
(`hidp_send_message() BT socket write error`, about 14 seconds into streaming, as seen on the tested hardware with a
second pad), after which the pad's HID device vanishes. The daemon prevents this with `hcitool lp <address> RSWITCH`. The
`hcitool` program is deprecated in BlueZ and some distributions no longer install it. Without it, **a pad may drop
after about 14 seconds of streaming**, a second pad first. `check` warns about it.

What was and was not done about it:

- **Implemented and tested on the target machine:** `hcitool lp <address> RSWITCH` (the daemon repeats it every 30 seconds
  while streaming), and `bluetoothctl disconnect` after a drop so one press of the PS button reconnects the pad.
- **Not implemented:** a replacement that works without `hcitool`. As far as we know BlueZ's D-Bus API and `btmgmt` do
  not offer a way to change one connection's link policy, and a raw HCI implementation could not be verified without the
  hardware. An untested code path that writes to the Bluetooth stack would be worse than a documented gap.

If your system lacks `hcitool`:

1. **Install it.** It has been part of BlueZ's deprecated tools. Find the package that has it on your distribution:
   `apt-file search bin/hcitool` (Debian, Ubuntu), `dnf provides '*/hcitool'` (Fedora), `pacman -F hcitool` (Arch).
   On some distributions it is in the main `bluez` package, on others in a separate "deprecated tools" package.
2. **Build it.** BlueZ's source builds the deprecated tools when configured with `--enable-deprecated` (untested by us).
   Whichever way you get it, the command to check is `hcitool lp <address>`: it must read `Link policy settings:
   RSWITCH` without `SNIFF` while the pad streams. `hcitool lp <address> RSWITCH` sets it (this is the tested command).
3. **Set the adapter's default policy** (untested by us; it comes from the same deprecated tool family, `hciconfig`, and
   applies to connections made afterwards): `sudo hciconfig hci0 lp rswitch`. Run it before the pad connects.
4. **Live with it:** use one pad at a time, and when a pad stops responding press the PS button to reconnect it (or
   `bluetoothctl disconnect <address>` first). If you do not want the daemon to try at all, set `disable_sniff` to `false`.

If you find a reliable method that does not need `hcitool`, please open an issue or a pull request with the exact commands
and the hardware you tried them on.

## Troubleshooting

See the README's troubleshooting section; on a desktop the usual suspects are: the pad is paired but in the wrong mode (no
hidraw node for it), the hidraw node is not writable (the udev rule), `pactl` cannot reach the audio server (run
`padspeaker check`, look at "runtime dir"), and the program that should play is still bound to the old output.
