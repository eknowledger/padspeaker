## What and why

<!-- What changed, and what problem it solves. Link the issue if there is one. If it came from a defect, say
     what you saw and what you expected instead. -->

## How it was tested

- [ ] `python -m pytest` passes (no hardware needed)
- [ ] New behaviour has tests, written first where practical; nothing in them needs a real pad or sound server
- [ ] Tried on hardware: <!-- pad model and HID id, system and sound server, what you heard; or "not applicable".
      A pad that is not in COMPATIBILITY.md earns a new row. -->

## Checklist

- [ ] Docs updated in the same change (README, `docs/`, `CHANGELOG.md`), including any new configuration key
- [ ] Only HID output reports `0x11` and `0x17` are written to a pad, and only to recognised pads
- [ ] If a byte on the wire changed, `docs/PROTOCOL.md` says so and a test pins the new bytes
- [ ] The installer still touches nothing outside `/userdata/system/padspeaker`, the service script and the logs
      (and `tests/test_deploy.py` covers any change to it)
- [ ] No personal data in code, tests, docs or examples (real Bluetooth addresses, host names, IP addresses); use
      `aa:bb:cc:dd:ee:ff`
- [ ] Compatible with Python 3.9

## Anything you are unsure about

<!-- Optional, and genuinely useful. A PR that names its own weak spot gets a better review. -->
