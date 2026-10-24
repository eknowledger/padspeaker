"""PulseAudio/PipeWire sink management through ``pactl``.

The daemon needs a handful of operations: list sinks, read the default sink,
create and remove a virtual (null) sink, and read a sink's volume and mute
state.  They are all done by running ``pactl`` with an argument list (never
through a shell) and a timeout, so a wedged audio server cannot hang the
daemon.  The process runner is injectable for tests.

On some systems (Batocera) the audio server's socket lives in an unusual
runtime directory, so :class:`SinkManager` sets ``XDG_RUNTIME_DIR`` in the
environment of every child process when a ``runtime_dir`` is given.
"""
from __future__ import annotations

import os
import re
import subprocess
from dataclasses import dataclass
from typing import Callable, Dict, List, Mapping, Optional, Sequence, Union

DEFAULT_TIMEOUT = 5.0

#: ``runner(argv, env, timeout)`` returns an object with ``returncode``,
#: ``stdout`` and ``stderr`` (text), like :class:`subprocess.CompletedProcess`.
Runner = Callable[[Sequence[str], Mapping[str, str], float], "subprocess.CompletedProcess"]

_TOKEN_RE = re.compile(r'([A-Za-z_][\w.\-]*)="((?:[^"\\]|\\.)*)"')
_UNESCAPE_RE = re.compile(r"\\(.)", re.DOTALL)
_NAME_RE = re.compile(r"^[A-Za-z0-9_.\-]+$")
_PERCENT_RE = re.compile(r"(\d+)%")
_SINK_NAME_ARG_RE = re.compile(r"""(?:^|\s)sink_name=(?:"([^"]*)"|'([^']*)'|(\S+))""")


class SinkError(RuntimeError):
    """Raised when a ``pactl`` call fails, times out or returns unusable output."""


@dataclass(frozen=True)
class Sink:
    """One audio output as reported by ``pactl list sinks-raw``.

    ``volume_percent`` is None when the listing did not include a volume.
    """

    name: str
    description: str
    volume_percent: Optional[int]
    muted: bool


def pulse_env(runtime_dir: Optional[str] = None,
              base: Optional[Mapping[str, str]] = None) -> Dict[str, str]:
    """Build the environment for audio tool child processes.

    ``LC_ALL=C`` keeps the output format stable; ``XDG_RUNTIME_DIR`` is set
    to ``runtime_dir`` when given, otherwise inherited.
    """
    env = dict(os.environ if base is None else base)
    env["LC_ALL"] = "C"
    if runtime_dir:
        env["XDG_RUNTIME_DIR"] = runtime_dir
    return env


def parse_sinks_raw(text: str) -> List[Sink]:
    """Parse ``pactl list sinks-raw`` output.

    Each line is a series of ``key="value"`` tokens; values may contain
    spaces, parentheses and backslash-escaped quotes.  Only the keys
    ``name``, ``description``, ``volume`` and ``mute``/``muted`` are used and
    their order does not matter.  Lines without a ``name`` are skipped.
    """
    sinks: List[Sink] = []
    for line in text.splitlines():
        fields: Dict[str, str] = {}
        for key, raw in _TOKEN_RE.findall(line):
            fields.setdefault(key, _UNESCAPE_RE.sub(r"\1", raw))
        name = fields.get("name")
        if not name:
            continue
        volume: Optional[int] = None
        match = re.match(r"\s*(\d+)", fields.get("volume", ""))
        if match:
            volume = int(match.group(1))
        mute = fields.get("mute", fields.get("muted", "no")).strip().lower()
        sinks.append(Sink(name, fields.get("description", ""), volume,
                          mute in ("yes", "true", "1")))
    return sinks


@dataclass(frozen=True)
class SinkDetails:
    """What the full ``pactl list sinks`` listing says about one sink."""

    name: str
    description: str
    properties: Dict[str, str]


_PROPERTY_RE = re.compile(r'^\s+([A-Za-z0-9_.\-:]+) = "((?:[^"\\]|\\.)*)"\s*$')


def parse_sink_details(text: str) -> Dict[str, SinkDetails]:
    """Parse the output of ``pactl list sinks`` (with ``LC_ALL=C``).

    The text is split into blocks at each ``Sink #`` line.  In every block the
    sink ``Name:`` and ``Description:`` are read (one tab of indentation) and
    the ``key = "value"`` lines below ``Properties:``.  Missing keys give an
    empty description or no properties; a block without a name is skipped.
    """
    details: Dict[str, SinkDetails] = {}
    blocks = re.split(r"(?m)^Sink #\d+\s*$", text)[1:]
    for block in blocks:
        name = ""
        description = ""
        properties: Dict[str, str] = {}
        in_properties = False
        for line in block.splitlines():
            if not line.strip():
                continue
            indent = len(line) - len(line.lstrip("\t"))
            if indent == 1:
                in_properties = line.strip() == "Properties:"
                key, _, value = line.strip().partition(":")
                if key == "Name" and not name:
                    name = value.strip()
                elif key == "Description" and not description:
                    description = value.strip()
            elif indent >= 2 and in_properties:
                match = _PROPERTY_RE.match(line)
                if match:
                    properties[match.group(1)] = _UNESCAPE_RE.sub(r"\1", match.group(2))
        if name:
            details[name] = SinkDetails(name, description, properties)
    return details


def quote_sink_properties(description: str) -> str:
    """Return the ``sink_properties=...`` module argument for a description.

    The argument string is parsed twice by the audio server: first as module
    arguments, then its ``sink_properties`` value as a property list.  The
    description is therefore single-quoted for the property list and the
    whole property assignment double-quoted for the module arguments, with
    backslash escapes at both levels.  For ``Foo (Bar)`` the result is
    ``sink_properties="device.description='Foo (Bar)'"``.  The argument is
    passed to ``pactl`` as one argv element, never through a shell.
    """
    if any(ord(ch) < 32 or ord(ch) == 127 for ch in description):
        raise SinkError("sink description contains control characters")
    inner = description.replace("\\", "\\\\").replace("'", "\\'")
    assignment = "device.description='%s'" % inner
    outer = assignment.replace("\\", "\\\\").replace('"', '\\"')
    return 'sink_properties="%s"' % outer


def _default_runner(argv: Sequence[str], env: Mapping[str, str], timeout: float):
    return subprocess.run(list(argv), env=dict(env), stdout=subprocess.PIPE,
                          stderr=subprocess.PIPE, universal_newlines=True,
                          timeout=timeout, check=False)


class SinkManager:
    """Thin, testable wrapper around ``pactl``.

    Args:
        runner: Callable ``(argv, env, timeout) -> CompletedProcess``; defaults
            to :func:`subprocess.run` without a shell.
        runtime_dir: Value for ``XDG_RUNTIME_DIR`` in the child environment
            (None keeps the inherited one).
        timeout: Seconds allowed for each call.
        pactl: Name or path of the ``pactl`` command.
    """

    def __init__(self, runner: Optional[Runner] = None, runtime_dir: Optional[str] = None,
                 timeout: float = DEFAULT_TIMEOUT, pactl: str = "pactl") -> None:
        self._runner = runner or _default_runner
        self._runtime_dir = runtime_dir
        self._timeout = timeout
        self._pactl = pactl

    def _run(self, *args: str) -> str:
        argv = [self._pactl] + list(args)
        env = pulse_env(self._runtime_dir)
        try:
            result = self._runner(argv, env, self._timeout)
        except subprocess.TimeoutExpired:
            raise SinkError("%s timed out after %.1fs" % (" ".join(argv), self._timeout))
        except OSError as exc:
            raise SinkError("cannot run %s: %s" % (self._pactl, exc))
        if result.returncode != 0:
            detail = (result.stderr or "").strip() or "exit status %s" % result.returncode
            raise SinkError("%s failed: %s" % (" ".join(argv), detail))
        return result.stdout or ""

    # -- queries -----------------------------------------------------------

    def list_sinks(self) -> List[Sink]:
        """Return all sinks known to the audio server."""
        return parse_sinks_raw(self._run("list", "sinks-raw"))

    def sink_details(self) -> Dict[str, SinkDetails]:
        """Return description and properties of every sink (one ``pactl list sinks`` call)."""
        return parse_sink_details(self._run("list", "sinks"))

    def sink_properties(self, name: str) -> Dict[str, str]:
        """Return the properties of one sink (``node.nick``, ``alsa.name``...); {} if unknown."""
        details = self.sink_details().get(name)
        return dict(details.properties) if details else {}

    def default_sink(self) -> Optional[str]:
        """Return the default sink name, or None when none is set."""
        for line in self._run("info").splitlines():
            key, sep, value = line.partition(":")
            if sep and key.strip() == "Default Sink":
                return value.strip() or None
        return None

    def sink_volume(self, name: str) -> int:
        """Return the sink volume in percent (the loudest channel; may exceed 100)."""
        out = self._run("get-sink-volume", name)
        percents = [int(p) for p in _PERCENT_RE.findall(out)]
        if not percents:
            raise SinkError("cannot parse volume of %r: %r" % (name, out.strip()))
        return max(percents)

    def sink_muted(self, name: str) -> bool:
        """Return True when the sink is muted."""
        out = self._run("get-sink-mute", name)
        match = re.search(r"Mute:\s*(\w+)", out)
        if not match:
            raise SinkError("cannot parse mute state of %r: %r" % (name, out.strip()))
        return match.group(1).lower() == "yes"

    # -- changes -----------------------------------------------------------

    def set_default_sink(self, name: str) -> None:
        """Make ``name`` the default sink (only used to fall back after a pad vanishes)."""
        if not name:
            raise SinkError("empty sink name")
        self._run("set-default-sink", name)

    def sink_states(self) -> Dict[str, str]:
        """Return ``{sink name: state}`` (``RUNNING``, ``IDLE``, ``SUSPENDED``...).

        Parsed from ``pactl list short sinks``; a sink that has a stream attached
        and active reads ``RUNNING``.
        """
        states: Dict[str, str] = {}
        for line in self._run("list", "short", "sinks").splitlines():
            parts = line.split("\t")
            if len(parts) >= 5 and parts[0].isdigit():
                states[parts[1]] = parts[-1].strip()
        return states

    def sink_state(self, name: str) -> Optional[str]:
        """Return one sink's state, or None when no such sink exists."""
        return self.sink_states().get(name)

    def _module_id_for_sink(self, name: str,
                            kinds: Sequence[str] = ("module-null-sink",)) -> Optional[int]:
        for line in self._run("list", "modules", "short").splitlines():
            parts = line.split("\t", 2)
            if len(parts) < 3 or parts[1] not in kinds or not parts[0].isdigit():
                continue
            match = _SINK_NAME_ARG_RE.search(parts[2])
            if match and (match.group(1) or match.group(2) or match.group(3)) == name:
                return int(parts[0])
        return None

    def create_null_sink(self, name: str, description: str) -> int:
        """Create a virtual sink and return its module id.

        Idempotent: when a sink called ``name`` already exists, the id of the
        ``module-null-sink`` instance behind it is returned instead.  Raises
        :class:`SinkError` if the sink exists but is not a null-sink module.
        """
        if not _NAME_RE.match(name or ""):
            raise SinkError("invalid sink name %r" % (name,))
        properties = quote_sink_properties(description)
        if any(s.name == name for s in self.list_sinks()):
            existing = self._module_id_for_sink(name)
            if existing is None:
                raise SinkError("sink %r exists but is not a null-sink module" % name)
            return existing
        out = self._run("load-module", "module-null-sink", "sink_name=" + name, properties)
        text = out.strip()
        if not text.isdigit():
            raise SinkError("unexpected output from load-module: %r" % text)
        return int(text)

    def create_combine_sink(self, name: str, description: str, slaves: Sequence[str]) -> int:
        """Create a combined sink playing to all ``slaves`` and return its module id.

        The slave list of a ``module-combine-sink`` is fixed when it is loaded;
        to change it, remove the sink and create it again.  Idempotent like
        :meth:`create_null_sink`: an existing sink of that name is returned as is
        (without checking its slaves).
        """
        if not _NAME_RE.match(name or ""):
            raise SinkError("invalid sink name %r" % (name,))
        if not slaves or any(not _NAME_RE.match(slave or "") for slave in slaves):
            raise SinkError("invalid slave sink list %r" % (list(slaves),))
        properties = quote_sink_properties(description)
        if any(s.name == name for s in self.list_sinks()):
            existing = self._module_id_for_sink(name, ("module-combine-sink",))
            if existing is None:
                raise SinkError("sink %r exists but is not a combine-sink module" % name)
            return existing
        out = self._run("load-module", "module-combine-sink", "sink_name=" + name,
                        "slaves=" + ",".join(slaves), properties)
        text = out.strip()
        if not text.isdigit():
            raise SinkError("unexpected output from load-module: %r" % text)
        return int(text)

    def remove_null_sink(self, module: Union[int, str]) -> None:
        """Unload a null or combine sink given its module id or its sink name.

        An unknown sink name is not an error (nothing to remove).
        """
        if isinstance(module, str) and not module.isdigit():
            found = self._module_id_for_sink(module, ("module-null-sink", "module-combine-sink"))
            if found is None:
                return
            module = found
        self._run("unload-module", str(int(module)))
