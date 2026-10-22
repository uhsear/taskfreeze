#!/usr/bin/env python3
"""taskfreeze - disable a set of Windows scheduled tasks for a maintenance window,
and put back exactly the ones it turned off.

The usual script records "the tasks I changed" in a manifest and enables the manifest
on the way out. Two things break that. A freeze that runs twice finds every task already
off, writes a manifest of zero tasks over the first one, and the thaw then reports
"0 of 0 restored" while the real jobs stay off. And a script that enables "everything
in scope" turns back on a task somebody switched off on purpose a year ago.

This tool refuses both. It will not write an empty manifest. It will not overwrite a
manifest that exists. It records a task only when it saw the task enabled and then saw
it disabled. The thaw touches only tasks in the manifest, and never a task that was
already off before the freeze. A manifest that has been thawed cannot be thawed again.

    python taskfreeze.py --self-test
    python taskfreeze.py --name "^[\\\\]Nightly[\\\\]" --manifest freeze1.json
    python taskfreeze.py --action "load[.]py" --manifest freeze1.json --apply
    python taskfreeze.py --status freeze1.json
    python taskfreeze.py --thaw freeze1.json
    python taskfreeze.py --thaw freeze1.json --apply
    python taskfreeze.py --name Nightly --manifest f.json --from-xml export.xml

Reads the scheduler with `schtasks /query /xml` and changes it with
`schtasks /change /tn NAME /disable` or `/enable`. Nothing is changed without --apply.
The one file it writes is the manifest. Local machine only. Disabling a task does not
stop an instance that is already running.

Exit codes: 0 done, 1 not everything was done or verified (a task failed to change,
did not read back as changed, or is missing), 2 the scheduler or a file could not be
read or written, 3 refused (nothing to freeze, a manifest already exists, an empty or
spent manifest), 64 usage error, 70 an unexpected crash.

Python 3.9+, standard library only.
"""

import argparse
import collections
import contextlib
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import traceback
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from xml.sax.saxutils import escape

__version__ = "1.0.0"

EXIT_OK = 0
EXIT_PARTIAL = 1
EXIT_IO = 2
EXIT_REFUSED = 3
EXIT_USAGE = 64
EXIT_CRASH = 70

MANIFEST_KIND = "taskfreeze-manifest"
MANIFEST_VERSION = 1
SCHEDULER_TIMEOUT = 120

Task = collections.namedtuple("Task", "name enabled actions")
FreezePlan = collections.namedtuple("FreezePlan", "disable leave_off refusal")
ThawPlan = collections.namedtuple("ThawPlan", "enable already_on missing refusal")


class SchedulerError(Exception):
    """The scheduler could not be read or changed."""


# ---------------------------------------------------------------------------
# Pure core. No I/O below this line until the scheduler classes.
# ---------------------------------------------------------------------------

# `schtasks /query /xml` prints each task as an XML comment holding its full path, then
# the task definition. The path is not inside the definition on every task, so it is read
# from the comment. Plain /xml puts an XML declaration between the two, and the form
# `/xml ONE` does not, so the declaration is optional. Verified on Windows 11 build 26100.
_BLOCK = re.compile(r"<!--(.*?)-->\s*(?:<\?xml[^>]*\?>\s*)?(<Task\b.*?</Task>)", re.S)
_TASK_OPEN = re.compile(r"<Task\b")


def _child(node, local):
    """First direct child of node with this local tag name, or None."""
    for child in node:
        if child.tag == local or child.tag.endswith("}" + local):
            return child
    return None


def _children(node, local):
    return [c for c in node if c.tag == local or c.tag.endswith("}" + local)]


def _xml_bool(text):
    """The four literals xs:boolean allows, or None for anything else."""
    value = (text or "").strip()  # xs:boolean collapses white space and is case sensitive
    if value in ("true", "1"):
        return True
    if value in ("false", "0"):
        return False
    return None


def _parse_block(name, body):
    try:
        root = ET.fromstring(body)
    except ET.ParseError as exc:
        raise ValueError("task %s has XML that does not parse: %s" % (name, exc))
    # Only Settings/Enabled is the task's state. A trigger has its own Enabled element,
    # and reading that one would report a live task as off.
    enabled = True
    settings = _child(root, "Settings")
    flag = _child(settings, "Enabled") if settings is not None else None
    if flag is not None:
        enabled = _xml_bool(flag.text)
        if enabled is None:
            raise ValueError("task %s has Settings/Enabled %r, not a boolean"
                             % (name, flag.text))
    commands = []
    actions = _child(root, "Actions")
    for exe in (_children(actions, "Exec") if actions is not None else []):
        command = _child(exe, "Command")
        arguments = _child(exe, "Arguments")
        text = ((command.text or "") if command is not None else "") + " " + \
               ((arguments.text or "") if arguments is not None else "")
        commands.append(text.strip())
    return Task(name, enabled, " ; ".join(commands))


def parse_tasks(text):
    """Task list from `schtasks /query /xml` output. Raises ValueError when the text is
    not a complete listing: no tasks, a task with no name comment, or a repeated name."""
    tasks = []
    seen = set()
    for match in _BLOCK.finditer(text):
        name = match.group(1).strip()
        if not name.startswith("\\"):
            raise ValueError("comment %r is not a task path" % name)
        if name.lower() in seen:
            raise ValueError("task %s is listed twice" % name)
        seen.add(name.lower())
        tasks.append(_parse_block(name, match.group(2)))
    if not tasks:
        raise ValueError("the listing holds no task definitions")
    opened = len(_TASK_OPEN.findall(text))
    if opened != len(tasks):
        raise ValueError("%d task definitions but %d named ones: a task with no name "
                         "comment would be missed by every selector"
                         % (opened, len(tasks)))
    return tasks


def compile_pattern(flag, text):
    """Case-insensitive regex, because Windows task names are. Empty text is refused: it
    would match every task."""
    if text is None:
        return None
    if not text.strip():
        raise ValueError("%s is empty and would match every task" % flag)
    try:
        return re.compile(text, re.I)
    except re.error as exc:
        hint = ""
        if text.endswith("\\"):
            # Windows command-line parsing halves the backslashes before a closing quote,
            # so "^\\Nightly\\" arrives as ^\Nightly\ and ends in a lone backslash.
            hint = " A pattern cannot end in a backslash; write [\\\\] for one."
        raise ValueError("%s is not a valid regular expression: %s.%s" % (flag, exc, hint))


def is_lossy(name):
    """A name the console code page could not carry. Task names cannot contain '?', so a
    '?' or U+FFFD means characters were replaced on the way in, and a /change on that
    name would address a different task or none."""
    return "?" in name or "\ufffd" in name


def select_tasks(tasks, name_re, action_re, except_re):
    """Tasks matching every given selector and not the exclusion."""
    picked = []
    for task in tasks:
        if name_re is not None and not name_re.search(task.name):
            continue
        if action_re is not None and not action_re.search(task.actions):
            continue
        if except_re is not None and except_re.search(task.name):
            continue
        picked.append(task)
    return picked


def _by_name(names):
    return sorted(names, key=lambda n: n.lower())


def plan_freeze(tasks, name_re, action_re, except_re, manifest_exists):
    """Decide what a freeze would do. refusal is None or the reason it must not run."""
    if name_re is None and action_re is None:
        raise ValueError("a freeze needs --name or --action; with neither it would "
                         "select every task")
    matched = select_tasks(tasks, name_re, action_re, except_re)
    disable = _by_name(t.name for t in matched if t.enabled)
    leave_off = _by_name(t.name for t in matched if not t.enabled)
    refusal = None
    if manifest_exists:
        refusal = ("the manifest file already exists. A second freeze must not "
                   "overwrite the record of the first. Thaw it, or choose a new path.")
    elif not matched:
        refusal = "the selectors matched no task. Nothing would be frozen."
    elif any(is_lossy(t.name) for t in matched):
        refusal = ("a matched task name holds a character the console code page "
                   "could not carry, so it cannot be addressed safely.")
    elif not disable:
        refusal = ("every matched task is already disabled, so the manifest would be "
                   "empty and a thaw would restore nothing. A freeze may still be in "
                   "force: check --status on its manifest.")
    return FreezePlan(disable, leave_off, refusal)


def build_manifest(plan, name_pattern, action_pattern, except_pattern, created):
    return {
        "kind": MANIFEST_KIND,
        "version": MANIFEST_VERSION,
        "tool_version": __version__,
        "created": created,
        "name_pattern": name_pattern,
        "action_pattern": action_pattern,
        "except_pattern": except_pattern,
        "count": len(plan.disable),
        "disabled": list(plan.disable),
        "already_off": list(plan.leave_off),
        "thawed": None,
    }


def manifest_problem(obj):
    """None when the manifest is usable, else the reason it is not."""
    if not isinstance(obj, dict):
        return "the manifest is not a JSON object"
    if obj.get("kind") != MANIFEST_KIND:
        return "the file is not a taskfreeze manifest"
    if obj.get("version") != MANIFEST_VERSION:
        return "manifest version %r is not the version %d this tool reads" % (
            obj.get("version"), MANIFEST_VERSION)
    for key in ("disabled", "already_off"):
        value = obj.get(key)
        if not isinstance(value, list):
            return "manifest field %s is not a list" % key
        for item in value:
            if not isinstance(item, str) or not item.startswith("\\"):
                return "manifest field %s holds %r, which is not a task path" % (
                    key, item)
        if len(set(v.lower() for v in value)) != len(value):
            return "manifest field %s lists a task twice" % key
    if not obj["disabled"]:
        return ("the manifest lists no task to restore. A freeze never writes one, "
                "so the file was edited or cut short.")
    if obj.get("count") != len(obj["disabled"]):
        return "manifest count %r does not match its %d listed task(s)" % (
            obj.get("count"), len(obj["disabled"]))
    if set(v.lower() for v in obj["disabled"]) & set(v.lower() for v in obj["already_off"]):
        return "a task is listed as both disabled by the freeze and off before it"
    thawed = obj.get("thawed")
    if thawed is not None and not (isinstance(thawed, str) and thawed):
        return "manifest field thawed is not a timestamp"
    return None


def _index(tasks):
    return dict((t.name.lower(), t) for t in tasks)


def plan_thaw(manifest, tasks):
    """What a thaw would do. Only names in manifest["disabled"] are ever considered, so a
    task that was off before the freeze cannot appear in enable."""
    problem = manifest_problem(manifest)
    if problem:
        return ThawPlan([], [], [], problem)
    if manifest.get("thawed"):
        return ThawPlan([], [], [], "this manifest was already thawed at %s. A second "
                        "thaw could re-enable a task someone has disabled since."
                        % manifest["thawed"])
    index = _index(tasks)
    enable, already_on, missing = [], [], []
    for name in manifest["disabled"]:
        task = index.get(name.lower())
        if task is None:
            missing.append(name)
        elif task.enabled:
            already_on.append(name)
        else:
            enable.append(name)
    return ThawPlan(_by_name(enable), _by_name(already_on), _by_name(missing), None)


def status_report(manifest, tasks):
    """(lines, drift). drift is True when a task is not in the state the manifest says:
    off for a live freeze, on for a thawed one, or gone."""
    index = _index(tasks)
    thawed = bool(manifest.get("thawed"))
    lines = []
    drift = False
    for name in _by_name(manifest["disabled"]):
        task = index.get(name.lower())
        if task is None:
            state, bad = "MISSING", True
        elif task.enabled:
            state, bad = "ON", not thawed
        else:
            state, bad = "OFF", thawed
        drift = drift or bad
        lines.append("%-8s %s%s" % (state, name, "  <-- drift" if bad else ""))
    for name in _by_name(manifest["already_off"]):
        task = index.get(name.lower())
        state = "gone" if task is None else ("on" if task.enabled else "off")
        note = {"off": "off before the freeze, off now",
                "on": "off before the freeze, on now; not this tool's doing",
                "gone": "off before the freeze, no longer exists"}[state]
        lines.append("%-8s %s  (%s)" % ("left", name, note))
    return lines, drift


def decode_output(data, codec):
    """Text from scheduler output or an export file. A BOM wins, then the codec, then
    UTF-8 with replacement, which marks a lost character as U+FFFD."""
    try:
        if data.startswith(b"\xff\xfe") or data.startswith(b"\xfe\xff"):
            return data.decode("utf-16")
        if data.startswith(b"\xef\xbb\xbf"):
            return data.decode("utf-8-sig")
        return data.decode(codec)
    except (LookupError, UnicodeDecodeError):
        return data.decode("utf-8", "replace")


def stamp():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------------------
# Scheduler access.
# ---------------------------------------------------------------------------

class Schtasks(object):
    """The Windows scheduler through schtasks.exe. cmd is the program and any leading
    arguments, as a list, so a path with spaces needs no quoting."""

    def __init__(self, cmd, codec=None, timeout=SCHEDULER_TIMEOUT):
        self.cmd = list(cmd)
        # schtasks writes to a pipe in the OEM code page, not the ANSI one.
        self.codec = codec or ("oem" if os.name == "nt" else "utf-8")
        self.timeout = timeout

    def _run(self, args):
        try:
            proc = subprocess.run(self.cmd + args, stdout=subprocess.PIPE,
                                  stderr=subprocess.PIPE, timeout=self.timeout)
        except OSError as exc:
            raise SchedulerError("cannot run %s: %s" % (self.cmd[0], exc))
        except subprocess.TimeoutExpired:
            raise SchedulerError("%s did not finish in %s seconds"
                                 % (self.cmd[0], self.timeout))
        if proc.returncode != 0:
            detail = decode_output(proc.stderr or proc.stdout, self.codec).strip()
            raise SchedulerError("%s %s exited %d: %s" % (
                self.cmd[0], " ".join(args[:1]), proc.returncode, detail))
        return decode_output(proc.stdout, self.codec)

    def list_tasks(self):
        try:
            return parse_tasks(self._run(["/query", "/xml"]))
        except ValueError as exc:
            raise SchedulerError("the scheduler listing is not usable: %s" % exc)

    def set_enabled(self, name, on):
        self._run(["/change", "/tn", name, "/enable" if on else "/disable"])


class Export(object):
    """A saved `schtasks /query /xml` listing. Read only: it lets a plan run anywhere."""

    def __init__(self, path, codec=None):
        self.path = path
        self.codec = codec or ("oem" if os.name == "nt" else "utf-8")

    def list_tasks(self):
        try:
            with open(self.path, "rb") as fh:
                return parse_tasks(decode_output(fh.read(), self.codec))
        except OSError as exc:
            raise SchedulerError("cannot read %s: %s" % (self.path, exc))
        except ValueError as exc:
            raise SchedulerError("%s is not a usable listing: %s" % (self.path, exc))


# ---------------------------------------------------------------------------
# Files.
# ---------------------------------------------------------------------------

def _dump(obj, fh):
    json.dump(obj, fh, indent=2, sort_keys=True)
    fh.write("\n")


def write_new(path, obj):
    """Create the file or fail. Never overwrites."""
    with open(path, "x", encoding="utf-8") as fh:
        _dump(obj, fh)


def write_replace(path, obj):
    """Replace the file whole or not at all."""
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        _dump(obj, fh)
    os.replace(tmp, path)


def read_manifest(path):
    """Parsed JSON. Raises OSError or ValueError."""
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


# ---------------------------------------------------------------------------
# Commands.
# ---------------------------------------------------------------------------

def say(text):
    print(text)


def warn(text):
    print(text, file=sys.stderr)


def _after(scheduler, what):
    """Read the scheduler back. None when it cannot be read."""
    try:
        return dict((t.name.lower(), t.enabled) for t in scheduler.list_tasks())
    except SchedulerError as exc:
        warn("error: cannot read the scheduler back after %s: %s" % (what, exc))
        return None


def _freeze(args, scheduler, tasks, patterns):
    plan = plan_freeze(tasks, patterns[0], patterns[1], patterns[2],
                       os.path.exists(args.manifest))
    for name in plan.leave_off:
        say("leave    %s  (already disabled, stays as it is)" % name)
    if plan.refusal:
        warn("refused: %s" % plan.refusal)
        say("matched %d enabled and %d already disabled task(s); nothing was changed."
            % (len(plan.disable), len(plan.leave_off)))
        return EXIT_REFUSED
    for name in plan.disable:
        say("disable  %s" % name)
    say("%d to disable, %d already off and left alone. Manifest: %s"
        % (len(plan.disable), len(plan.leave_off), args.manifest))
    if not args.apply:
        say("nothing changed. Add --apply to disable them.")
        return EXIT_OK
    manifest = build_manifest(plan, args.name, args.action, args.except_pattern, stamp())
    # Written before the first change, so a crash half way never leaves a disabled task
    # with no record. It is trimmed to what was verified once the loop ends.
    try:
        write_new(args.manifest, manifest)
    except OSError as exc:
        warn("error: cannot create the manifest, so nothing was disabled: %s" % exc)
        return EXIT_IO
    failed = {}
    for name in plan.disable:
        try:
            scheduler.set_enabled(name, False)
            say("disabled %s" % name)
        except SchedulerError as exc:
            failed[name] = str(exc)
            warn("FAILED   %s: %s" % (name, exc))
    state = _after(scheduler, "the freeze")
    if state is None:
        warn("the manifest lists every task that was attempted. Check --status.")
        return EXIT_IO
    # The read-back decides, not the exit status: a /change that errored after taking
    # effect left the task off, and leaving it out of the manifest would strand it.
    done = [n for n in plan.disable if state.get(n.lower()) is False]
    for name in plan.disable:
        if name in failed and name in done:
            warn("RECORDED %s: the change reported an error but the task reads disabled"
                 % name)
        elif name not in failed and name not in done:
            warn("UNVERIFIED %s: the change succeeded but the task still reads enabled"
                 % name)
    if not done:
        os.remove(args.manifest)
        warn("nothing was disabled, so no manifest was kept.")
        return EXIT_PARTIAL
    if len(done) != len(plan.disable):
        manifest["disabled"] = done
        manifest["count"] = len(done)
        try:
            write_replace(args.manifest, manifest)
        except OSError as exc:
            warn("error: cannot trim the manifest: %s. It lists every task attempted."
                 % exc)
            return EXIT_IO
    say("froze %d of %d task(s). Manifest: %s" % (len(done), len(plan.disable),
                                                   args.manifest))
    return EXIT_OK if len(done) == len(plan.disable) else EXIT_PARTIAL


def _thaw(args, scheduler, tasks, manifest):
    plan = plan_thaw(manifest, tasks)
    if plan.refusal:
        warn("refused: %s" % plan.refusal)
        return EXIT_REFUSED
    for name in plan.enable:
        say("enable   %s" % name)
    for name in plan.already_on:
        say("on       %s  (already enabled, nothing to do)" % name)
    for name in plan.missing:
        warn("MISSING  %s  (no such task now)" % name)
    for name in _by_name(manifest["already_off"]):
        say("left     %s  (off before the freeze, stays off)" % name)
    if not args.apply:
        say("nothing changed. Add --apply to enable %d task(s)." % len(plan.enable))
        return EXIT_PARTIAL if plan.missing else EXIT_OK
    failed = {}
    for name in plan.enable:
        try:
            scheduler.set_enabled(name, True)
            say("enabled  %s" % name)
        except SchedulerError as exc:
            failed[name] = str(exc)
            warn("FAILED   %s: %s" % (name, exc))
    state = _after(scheduler, "the thaw")
    if state is None:
        return EXIT_IO
    restored = [n for n in plan.enable if state.get(n.lower()) is True]
    for name in plan.enable:
        if name in failed and name in restored:
            warn("RECORDED %s: the change reported an error but the task reads enabled"
                 % name)
        elif name not in failed and name not in restored:
            warn("UNVERIFIED %s: the change succeeded but the task still reads disabled"
                 % name)
    total = len(manifest["disabled"])
    back = len(restored) + len(plan.already_on)
    say("%d of %d task(s) in the manifest are enabled: %d enabled now, %d already on, "
        "%d missing" % (back, total, len(restored), len(plan.already_on),
                        len(plan.missing)))
    if back != total:
        warn("the manifest was not marked thawed, so a re-run retries the rest.")
        return EXIT_PARTIAL
    manifest["thawed"] = stamp()
    try:
        write_replace(args.thaw, manifest)
    except OSError as exc:
        warn("error: the tasks are enabled but the manifest could not be marked "
             "thawed: %s" % exc)
        return EXIT_IO
    return EXIT_OK


def _status(manifest, tasks):
    problem = manifest_problem(manifest)
    if problem:
        warn("refused: %s" % problem)
        return EXIT_REFUSED
    lines, drift = status_report(manifest, tasks)
    for line in lines:
        say(line)
    say("%s: %d task(s) in the manifest%s" % (
        "DRIFT" if drift else "ok", len(manifest["disabled"]),
        ", thawed at %s" % manifest["thawed"] if manifest.get("thawed") else ""))
    return EXIT_PARTIAL if drift else EXIT_OK


# ---------------------------------------------------------------------------
# Command line.
# ---------------------------------------------------------------------------

def build_parser():
    ap = argparse.ArgumentParser(
        prog="taskfreeze", allow_abbrev=False,
        description="Disable scheduled tasks for a window and restore exactly those.",
        epilog="Nothing is changed without --apply. Exit: 0 done, 1 partial or drift, "
               "2 I/O, 3 refused, 64 usage, 70 crash.")
    ap.add_argument("--name", help="regex on the task path, e.g. \"^[\\\\]Nightly[\\\\]\"")
    ap.add_argument("--action", help="regex on the task's command and arguments")
    ap.add_argument("--except", dest="except_pattern",
                    help="regex on the task path; matching tasks are never selected")
    ap.add_argument("--manifest", help="where a freeze writes its record; must not exist")
    ap.add_argument("--thaw", metavar="MANIFEST", help="restore the tasks in this manifest")
    ap.add_argument("--status", metavar="MANIFEST",
                    help="compare the scheduler with this manifest; read only")
    ap.add_argument("--from-xml", dest="from_xml", metavar="FILE",
                    help="read a saved `schtasks /query /xml` listing instead of the "
                         "scheduler; read only, so it cannot be combined with --apply")
    ap.add_argument("--schtasks", default="schtasks", metavar="PROGRAM",
                    help="the schtasks program (default: schtasks)")
    ap.add_argument("--apply", action="store_true",
                    help="change the scheduler and write the manifest")
    ap.add_argument("--self-test", dest="self_test", action="store_true",
                    help="run the offline assertions and exit")
    return ap


def usage_problem(args):
    """None when the flag combination makes sense, else why not."""
    freeze = any(v is not None for v in (args.name, args.action, args.except_pattern,
                                         args.manifest))
    modes = [freeze, args.thaw is not None, args.status is not None]
    if modes.count(True) == 0:
        return "nothing to do. Give --name or --action with --manifest, or --thaw, or " \
               "--status, or --self-test."
    if modes.count(True) > 1:
        return "a freeze (--name, --action, --manifest), --thaw and --status are " \
               "separate modes. Use one."
    if freeze and (args.name is None and args.action is None):
        return "a freeze needs --name or --action."
    if freeze and args.manifest is None:
        return "a freeze needs --manifest, the file that records what it disabled."
    if args.apply and args.status is not None:
        return "--status only reads. --apply does not apply to it."
    if args.apply and args.from_xml is not None:
        return "--from-xml is a saved listing and cannot be changed, so it cannot be " \
               "combined with --apply."
    return None


def _tolerate_odd_characters(streams):
    """A task name the console cannot print must not turn a verdict into a traceback."""
    for stream in streams:
        try:
            stream.reconfigure(errors="replace")
        except (AttributeError, ValueError):
            pass


def main(argv=None, scheduler=None):
    """Run the CLI. scheduler is a test seam: an object with list_tasks and set_enabled.
    An error nobody anticipated exits 70, because 1 means 'not everything was done'."""
    _tolerate_odd_characters((sys.stdout, sys.stderr))
    try:
        return _run(sys.argv[1:] if argv is None else argv, scheduler)
    except Exception as exc:
        warn("error: unexpected %s: %s. This is a fault in the tool, not a finding."
             % (type(exc).__name__, exc))
        traceback.print_exc()
        return EXIT_CRASH


def _run(argv, scheduler):
    try:
        args = build_parser().parse_args(argv)
    except SystemExit as exc:
        # argparse exits 2 on a flag it refuses, and 2 here means an I/O error. A
        # mistyped --ap is a usage error.
        return EXIT_USAGE if exc.code == 2 else exc.code
    if args.self_test:
        return self_test()
    problem = usage_problem(args)
    if problem:
        warn("error: %s" % problem)
        return EXIT_USAGE
    try:
        patterns = (compile_pattern("--name", args.name),
                    compile_pattern("--action", args.action),
                    compile_pattern("--except", args.except_pattern))
    except ValueError as exc:
        warn("error: %s" % exc)
        return EXIT_USAGE
    if scheduler is None:
        scheduler = Export(args.from_xml) if args.from_xml else Schtasks([args.schtasks])
    manifest = None
    path = args.thaw if args.thaw is not None else args.status
    if path is not None:
        try:
            manifest = read_manifest(path)
        except (OSError, ValueError) as exc:
            warn("error: cannot read the manifest %s: %s" % (path, exc))
            return EXIT_IO
    try:
        tasks = scheduler.list_tasks()
    except SchedulerError as exc:
        warn("error: %s" % exc)
        return EXIT_IO
    if args.thaw is not None:
        return _thaw(args, scheduler, tasks, manifest)
    if args.status is not None:
        return _status(manifest, tasks)
    return _freeze(args, scheduler, tasks, patterns)


# ---------------------------------------------------------------------------
# Self-test support. Everything below is exercised only by --self-test.
# ---------------------------------------------------------------------------

def fixture_xml(specs, declaration=True):
    """A listing in the shape `schtasks /query /xml` prints, for the self-test. Each spec
    is a dict: name, enabled (default True), actions (list of (command, arguments)).
    Every task carries a trigger with its own Enabled element, as real tasks do. With
    declaration False the listing has the shape of `/xml ONE`."""
    out = ["\r\n<Tasks>\r\n"]
    for spec in specs:
        out.append("\r\n<!-- %s -->\r\n" % spec["name"])
        if declaration:
            out.append('<?xml version="1.0" encoding="UTF-16"?>\r\n')
        out.append('<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/'
                   '2004/02/mit/task">\r\n  <Settings>\r\n')
        if not spec.get("enabled", True):
            out.append("    <Enabled>false</Enabled>\r\n")
        out.append("    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>\r\n"
                   "  </Settings>\r\n  <Triggers>\r\n    <TimeTrigger>\r\n"
                   "      <Enabled>true</Enabled>\r\n    </TimeTrigger>\r\n"
                   "  </Triggers>\r\n  <Actions Context=\"Author\">\r\n")
        for command, arguments in spec.get("actions", [("cmd", "/c exit")]):
            out.append("    <Exec>\r\n      <Command>%s</Command>\r\n" % escape(command))
            if arguments:
                out.append("      <Arguments>%s</Arguments>\r\n" % escape(arguments))
            out.append("    </Exec>\r\n")
        out.append("  </Actions>\r\n</Task>\r\n")
    out.append("\r\n</Tasks>\r\n")
    return "".join(out)


# A stand-in schtasks.exe. It keeps its tasks in a JSON state file and answers the two
# calls the tool makes. "fail" lists tasks whose /change exits 1, "ignore" lists tasks
# whose /change exits 0 and changes nothing, "partial" lists tasks whose /change takes
# effect and then exits 1, "hang" makes every call sleep.
STUB_SOURCE = '''import json, sys, time
sys.path.insert(0, %r)
import taskfreeze
state_path = sys.argv[1]
argv = sys.argv[2:]
with open(state_path) as fh:
    state = json.load(fh)
if state.get("hang"):
    time.sleep(30)
if argv == ["/query", "/xml"]:
    sys.stdout.buffer.write(taskfreeze.fixture_xml(state["tasks"]).encode("utf-8"))
    sys.exit(0)
if len(argv) == 4 and argv[0] == "/change" and argv[1] == "/tn" and argv[3] in (
        "/enable", "/disable"):
    name = argv[2]
    found = [t for t in state["tasks"] if t["name"].lower() == name.lower()]
    if not found:
        sys.stderr.write("ERROR: The specified task name does not exist.\\n")
        sys.exit(1)
    if name in state.get("fail", []):
        sys.stderr.write("ERROR: Access is denied.\\n")
        sys.exit(1)
    if name not in state.get("ignore", []):
        found[0]["enabled"] = argv[3] == "/enable"
    with open(state_path, "w") as fh:
        json.dump(state, fh)
    if name in state.get("partial", []):
        sys.stderr.write("ERROR: The task was changed but the call failed.\\n")
        sys.exit(1)
    sys.exit(0)
sys.stderr.write("ERROR: Invalid argument or option.\\n")
sys.exit(1)
'''


def make_harness():
    """check, raises and report. report returns the process exit code."""
    passed = [0]
    failed = []

    def check(cond, label):
        if cond:
            passed[0] += 1
            print("PASS  %s" % label)
        else:
            failed.append(label)
            print("FAIL  %s" % label)

    def raises(fn, label, exc_type=ValueError):
        try:
            fn()
        except exc_type:
            check(True, label)
        except Exception as exc:
            check(False, "%s (wrong exception %r)" % (label, exc))
        else:
            check(False, "%s (no error raised)" % label)

    def report():
        total = passed[0] + len(failed)
        print("-" * 68)
        if failed:
            print("%d assertions, %d failed:" % (total, len(failed)))
            for label in failed:
                print("  FAILED  %s" % label)
            return 1
        print("%d assertions, 0 failed" % total)
        return 0

    return check, raises, report


def self_test():
    """Run the assertions in a temp directory that is removed even when one raises."""
    tmp = tempfile.mkdtemp(prefix="taskfreeze-selftest-")
    try:
        return _self_test(tmp)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _self_test(tmp):
    check, raises, report = make_harness()
    print("taskfreeze self-test: temporary files and a stand-in schtasks, no network")
    print("-" * 68)

    def spec(name, enabled=True, actions=None):
        out = {"name": name, "enabled": enabled}
        if actions is not None:
            out["actions"] = actions
        return out

    def names(tasks):
        return [t.name for t in tasks]

    # --- parse_tasks -----------------------------------------------------------
    listing = fixture_xml([
        spec("\\Nightly\\Load", True, [("C:\\etl\\load.exe", "--full")]),
        spec("\\Nightly\\Reconcile", False),
        spec("\\Other\\Backup", True, [("a.exe", ""), ("b.exe", "x & y")]),
        spec("\\NoAction", True, []),
    ])
    tasks = parse_tasks(listing)
    check(names(tasks) == ["\\Nightly\\Load", "\\Nightly\\Reconcile", "\\Other\\Backup",
                           "\\NoAction"], "parse reads every task name from the comments")
    check([t.enabled for t in tasks] == [True, False, True, True],
          "parse reads Settings/Enabled")
    check(tasks[0].actions == "C:\\etl\\load.exe --full", "an action is command then arguments")
    check(tasks[2].actions == "a.exe ; b.exe x & y",
          "several actions join with a semicolon and an escaped ampersand is read back")
    check(tasks[3].actions == "", "a task with no action has empty action text")
    check(all(t.enabled for t in parse_tasks(fixture_xml([spec("\\T")]))),
          "a task whose Settings has no Enabled element is enabled, the schema default")
    check(parse_tasks(fixture_xml([spec("\\T", False)]))[0].enabled is False,
          "a disabled task whose triggers all say Enabled true is still disabled  "
          "<-- pinned defect")
    body = ('<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/'
            'task"><Settings><Enabled>%s</Enabled></Settings></Task>')
    for text, want in (("true", True), ("false", False), ("1", True), ("0", False),
                       (" false ", False)):
        got = parse_tasks("<!-- \\X -->\n" + body % text)[0].enabled
        check(got is want, "Settings/Enabled %r reads as %s" % (text, want))
    raises(lambda: parse_tasks("<!-- \\X -->\n" + body % "maybe"),
           "Settings/Enabled 'maybe' is refused, not guessed")
    raises(lambda: parse_tasks("<!-- \\X -->\n" + body % "FALSE"),
           "Settings/Enabled 'FALSE' is refused, because xs:boolean is case sensitive and a "
           "guess would read a disabled task as enabled")
    raises(lambda: parse_tasks("<!-- \\X -->\n" + body % ""),
           "an empty Settings/Enabled is refused")
    nosettings = '<Task xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task"/>'
    check(parse_tasks("<!-- \\X -->\n" + nosettings.replace("/>", "></Task>"))[0].enabled,
          "a task with no Settings element is enabled")
    raises(lambda: parse_tasks(""), "an empty listing is refused, not read as zero tasks  "
           "<-- pinned defect")
    raises(lambda: parse_tasks("<Tasks></Tasks>"), "a listing with no definition is refused")
    raises(lambda: parse_tasks("<!-- \\A -->\n<Task><Settings></Task>"),
           "a definition that is not XML is refused with the task name")
    raises(lambda: parse_tasks("<!-- no slash -->\n<Task></Task>"),
           "a comment that is not a task path is refused")
    raises(lambda: parse_tasks(listing + fixture_xml([spec("\\NIGHTLY\\load")])),
           "a repeated name, whatever its case, is refused")
    raises(lambda: parse_tasks(listing + "\n<Task version='1.2'></Task>"),
           "a definition with no name comment is refused, because no selector could see it"
           "  <-- pinned defect")
    raises(lambda: parse_tasks('<!DOCTYPE x [<!ENTITY e "true">]>\n<!-- \\A -->\n'
                               '<Task><Settings><Enabled>&e;</Enabled></Settings></Task>'),
           "an entity reference does not expand, because the DOCTYPE is outside the block")
    check(names(parse_tasks(fixture_xml([spec("\\A"), spec("\\B", False)], False)))
          == ["\\A", "\\B"] and "<?xml" in fixture_xml([spec("\\A")]) and
          "<?xml" not in fixture_xml([spec("\\A")], False),
          "a listing is read with or without the XML declaration between comment and task"
          "  <-- pinned defect")
    uni = parse_tasks(fixture_xml([spec("\\Caf\u00e9")]))
    check(uni[0].name == "\\Caf\u00e9", "a non-ASCII name survives the parse")

    # --- patterns and selection ------------------------------------------------
    raises(lambda: compile_pattern("--name", ""), "an empty pattern is refused  "
           "<-- pinned defect")
    raises(lambda: compile_pattern("--name", "   "), "a blank pattern is refused")
    raises(lambda: compile_pattern("--name", "(unclosed"), "a broken regex is refused")
    check(compile_pattern("--name", None) is None, "no pattern compiles to None")
    try:
        compile_pattern("--name", "^\\Nightly\\")
    except ValueError as exc:
        check("write [\\\\]" in str(exc),
              "a pattern that ends in a lone backslash gets the Windows quoting hint")
    check(compile_pattern("--name", "^[\\\\]Nightly[\\\\]").search("\\Nightly\\Load")
          is not None, "[\\\\] matches one backslash, and survives Windows quote parsing")
    check(compile_pattern("--name", "abc").search("xABCx") is not None,
          "patterns ignore case, as Windows task names do")
    name_re = compile_pattern("--name", "^\\\\nightly\\\\")
    act_re = compile_pattern("--action", "load")
    exc_re = compile_pattern("--except", "reconcile")
    check(names(select_tasks(tasks, name_re, None, None)) ==
          ["\\Nightly\\Load", "\\Nightly\\Reconcile"], "--name selects by task path")
    check(names(select_tasks(tasks, None, act_re, None)) == ["\\Nightly\\Load"],
          "--action selects by command text")
    check(names(select_tasks(tasks, name_re, act_re, None)) == ["\\Nightly\\Load"],
          "--name and --action must both match")
    check(names(select_tasks(tasks, name_re, None, exc_re)) == ["\\Nightly\\Load"],
          "--except removes a task that --name selected")
    check(select_tasks(tasks, compile_pattern("--name", "zzz"), None, None) == [],
          "a selector that matches nothing selects nothing")

    # --- plan_freeze -----------------------------------------------------------
    plan = plan_freeze(tasks, name_re, None, None, False)
    check(plan.refusal is None and plan.disable == ["\\Nightly\\Load"],
          "a freeze disables the enabled matched task")
    check(plan.leave_off == ["\\Nightly\\Reconcile"],
          "a matched task that is already off is left alone, never disabled again  "
          "<-- pinned defect")
    allon = parse_tasks(fixture_xml([spec("\\b"), spec("\\a"), spec("\\C")]))
    check(plan_freeze(allon, compile_pattern("--name", "."), None, None,
                      False).disable == ["\\a", "\\b", "\\C"],
          "the disable list is sorted without regard to case")
    alloff = parse_tasks(fixture_xml([spec("\\X1", False), spec("\\X2", False)]))
    plan = plan_freeze(alloff, compile_pattern("--name", "^.X"), None, None, False)
    check(plan.refusal is not None and "empty" in plan.refusal and plan.disable == [],
          "a freeze whose matches are all already off is REFUSED, because its manifest "
          "would be empty  <-- pinned defect")
    plan = plan_freeze(tasks, name_re, None, None, True)
    check(plan.refusal is not None and "already exists" in plan.refusal,
          "a freeze onto a manifest that exists is REFUSED  <-- pinned defect")
    check(plan_freeze(alloff, compile_pattern("--name", "^.X"), None, None,
                      True).refusal.startswith("the manifest file already exists"),
          "an existing manifest is named before the empty-manifest reason")
    plan = plan_freeze(tasks, compile_pattern("--name", "zzz"), None, None, False)
    check(plan.refusal is not None and "no task" in plan.refusal,
          "a selector that matches nothing is REFUSED, not reported as a clean freeze")
    lossy = [Task("\\Caf?", True, ""), Task("\\Fine", True, "")]
    plan = plan_freeze(lossy, compile_pattern("--name", "."), None, None, False)
    check(plan.refusal is not None and "character" in plan.refusal,
          "a matched name that lost a character is REFUSED")
    check(plan_freeze(lossy, compile_pattern("--name", "fine"), None, None,
                      False).refusal is None,
          "a lossy name that no selector matched does not block the freeze")
    check(is_lossy("\\a\ufffdb") and is_lossy("\\a?b") and not is_lossy("\\ab"),
          "is_lossy flags a replacement character and a question mark")
    raises(lambda: plan_freeze(tasks, None, None, exc_re, False),
           "a freeze with no --name and no --action is refused, not 'everything'"
           "  <-- pinned defect")

    # --- manifest --------------------------------------------------------------
    plan = plan_freeze(tasks, name_re, None, None, False)
    good = build_manifest(plan, "^x", None, "y", "2026-01-01T00:00:00Z")
    check(manifest_problem(good) is None, "a built manifest passes its own validation")
    check(good["count"] == 1 and good["disabled"] == ["\\Nightly\\Load"]
          and good["already_off"] == ["\\Nightly\\Reconcile"] and good["thawed"] is None,
          "the manifest records what was disabled, what was already off, and not thawed")
    check(json.loads(json.dumps(good)) == good, "the manifest survives a JSON round trip")

    def mutated(**changes):
        obj = json.loads(json.dumps(good))
        obj.update(changes)
        return obj

    for label, obj, word in (
            ("a list", [], "object"),
            ("another kind", mutated(kind="x"), "not a taskfreeze"),
            ("a future version", mutated(version=2), "version"),
            ("disabled that is not a list", mutated(disabled="x"), "not a list"),
            ("already_off that is not a list", mutated(already_off=None), "not a list"),
            ("an entry that is not a string", mutated(disabled=[3], count=1), "task path"),
            ("an entry with no leading backslash", mutated(disabled=["x"], count=1),
             "task path"),
            ("an empty disabled list", mutated(disabled=[], count=0), "no task"),
            ("a count that disagrees", mutated(count=5), "count"),
            ("a repeated entry", mutated(disabled=["\\a", "\\A"], count=2), "twice"),
            ("a task in both lists", mutated(already_off=["\\nightly\\load"]), "both"),
            ("thawed that is not a timestamp", mutated(thawed=5), "thawed")):
        problem = manifest_problem(obj)
        check(problem is not None and word in problem,
              "a manifest with %s is refused" % label)
    check(manifest_problem(mutated(thawed="2026-01-02T00:00:00Z")) is None,
          "a thawed manifest is still a valid manifest")

    # --- plan_thaw -------------------------------------------------------------
    now_tasks = [Task("\\Nightly\\Load", False, ""), Task("\\Nightly\\Reconcile", False, ""),
                 Task("\\Unrelated\\Old", False, "")]
    thaw = plan_thaw(good, now_tasks)
    check(thaw.refusal is None and thaw.enable == ["\\Nightly\\Load"],
          "a thaw enables the task the freeze disabled")
    check("\\Nightly\\Reconcile" not in thaw.enable and "\\Unrelated\\Old" not in thaw.enable,
          "a task that was off before the freeze is never enabled, listed or not  "
          "<-- pinned defect")
    thaw = plan_thaw(good, [Task("\\NIGHTLY\\LOAD", False, "")])
    check(thaw.enable == ["\\Nightly\\Load"], "a thaw matches task names without regard to case")
    thaw = plan_thaw(good, [Task("\\Nightly\\Load", True, "")])
    check(thaw.already_on == ["\\Nightly\\Load"] and thaw.enable == [],
          "a task someone already re-enabled is not touched")
    thaw = plan_thaw(good, [Task("\\Other", True, "")])
    check(thaw.missing == ["\\Nightly\\Load"], "a task that no longer exists is reported missing")
    thaw = plan_thaw(mutated(disabled=[], count=0), now_tasks)
    check(thaw.refusal is not None and thaw.enable == [],
          "a thaw of an empty manifest is REFUSED instead of reporting 0 of 0 restored  "
          "<-- pinned defect")
    thaw = plan_thaw(mutated(thawed="2026-01-02T00:00:00Z"), now_tasks)
    check(thaw.refusal is not None and "already thawed" in thaw.refusal,
          "a manifest that was already thawed is REFUSED  <-- pinned defect")
    check(plan_thaw(mutated(count=9), now_tasks).refusal is not None,
          "a manifest whose count disagrees with its list is refused")

    # --- status_report ---------------------------------------------------------
    lines, drift = status_report(good, now_tasks)
    check(not drift and lines[0].startswith("OFF") and "off before the freeze, off now"
          in lines[1], "status reports a frozen task OFF and a pre-off task as left alone")
    lines, drift = status_report(good, [Task("\\Nightly\\Load", True, "")])
    check(drift and "<-- drift" in lines[0] and lines[0].startswith("ON"),
          "a live freeze whose task is back on is drift")
    lines, drift = status_report(good, [])
    check(drift and lines[0].startswith("MISSING") and "no longer exists" in lines[1],
          "a missing frozen task is drift, and a missing pre-off task is noted")
    thawed = mutated(thawed="2026-01-02T00:00:00Z")
    check(not status_report(thawed, [Task("\\Nightly\\Load", True, "")])[1],
          "a thawed manifest whose task is on is not drift")
    check(status_report(thawed, now_tasks)[1], "a thawed manifest whose task is off is drift")
    lines, drift = status_report(good, [Task("\\Nightly\\Load", False, ""),
                                        Task("\\Nightly\\Reconcile", True, "")])
    check(not drift and "on now; not this tool's doing" in lines[1],
          "a pre-off task found on is noted but is not this tool's drift")

    # --- decode_output and stamp -----------------------------------------------
    check(decode_output("caf\u00e9".encode("utf-16"), "ascii") == "caf\u00e9",
          "a UTF-16 BOM is honoured before the codec")
    check(decode_output("caf\u00e9".encode("utf-16-be"), "ascii") != "x" and
          decode_output(b"\xfe\xff\x00a", "ascii") == "a", "a big-endian UTF-16 BOM is honoured")
    check(decode_output(b"\xef\xbb\xbfabc", "ascii") == "abc", "a UTF-8 BOM is dropped")
    check(decode_output(b"caf\x82", "cp437") == "caf\u00e9", "the OEM code page decodes its bytes")
    check(decode_output(b"caf\xc3\xa9", "no-such-codec") == "caf\u00e9",
          "an unknown codec falls back to UTF-8")
    check(decode_output(b"caf\xff", "ascii") == "caf\ufffd",
          "an undecodable byte becomes U+FFFD, which is_lossy then catches")
    check(decode_output(b"\xff\xfe\x00", "ascii") == "\ufffd" * 0 + decode_output(
        b"\xff\xfe\x00", "ascii"), "a broken UTF-16 stream falls back instead of raising")
    check(re.match(r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$", stamp()) is not None,
          "stamp is an ISO UTC timestamp")

    # --- the stand-in schtasks, through subprocess -----------------------------
    stub = os.path.join(tmp, "stub_schtasks.py")
    with open(stub, "w", encoding="utf-8") as fh:
        fh.write(STUB_SOURCE % os.path.dirname(os.path.abspath(__file__)))
    state_path = os.path.join(tmp, "state.json")

    def put_state(specs, **extra):
        data = {"tasks": specs}
        data.update(extra)
        with open(state_path, "w") as fh:
            json.dump(data, fh)

    def get_state():
        with open(state_path) as fh:
            return json.load(fh)

    def enabled_of(name):
        return [t["enabled"] for t in get_state()["tasks"] if t["name"] == name][0]

    def sched(**kw):
        return Schtasks([sys.executable, stub, state_path], codec="utf-8", **kw)

    base = [spec("\\Nightly\\Load", True, [("etl.exe", "--secret-canary-4417")]),
            spec("\\Nightly\\Reconcile", False), spec("\\Nightly\\Compress"),
            spec("\\Other\\Backup"), spec("\\Caf\u00e9")]
    put_state(base)
    listed = sched().list_tasks()
    check(len(listed) == 5 and listed[1].enabled is False,
          "Schtasks.list_tasks reads a real subprocess listing")
    check(listed[4].name == "\\Caf\u00e9", "a non-ASCII task name crosses the subprocess pipe")
    sched().set_enabled("\\Other\\Backup", False)
    check(enabled_of("\\Other\\Backup") is False, "set_enabled(False) issues /change /disable")
    sched().set_enabled("\\Other\\Backup", True)
    check(enabled_of("\\Other\\Backup") is True, "set_enabled(True) issues /change /enable")
    raises(lambda: sched().set_enabled("\\No\\Such", False),
           "a /change that exits 1 raises SchedulerError", SchedulerError)
    try:
        sched().set_enabled("\\No\\Such", False)
    except SchedulerError as exc:
        check("exited 1" in str(exc) and "does not exist" in str(exc),
              "the error names the exit code and the scheduler's own message")
    raises(lambda: Schtasks([os.path.join(tmp, "absent.exe")]).list_tasks(),
           "a missing program raises SchedulerError", SchedulerError)
    put_state(base, hang=True)
    raises(lambda: sched(timeout=0.5).list_tasks(),
           "a call that never returns times out as SchedulerError", SchedulerError)
    put_state([])
    raises(lambda: sched().list_tasks(), "an empty scheduler listing raises SchedulerError",
           SchedulerError)
    check(Schtasks(["x"]).codec == ("oem" if os.name == "nt" else "utf-8"),
          "the default codec is the OEM page on Windows and UTF-8 elsewhere")

    export = os.path.join(tmp, "export.xml")
    with open(export, "w", encoding="utf-8") as fh:
        fh.write(fixture_xml(base))
    check(len(Export(export, codec="utf-8").list_tasks()) == 5,
          "Export reads a saved listing")
    with open(export, "wb") as fh:
        fh.write(fixture_xml(base).encode("utf-16"))
    check(len(Export(export).list_tasks()) == 5, "Export reads a UTF-16 listing")
    raises(lambda: Export(os.path.join(tmp, "nope.xml")).list_tasks(),
           "a missing export raises SchedulerError", SchedulerError)
    with open(export, "w") as fh:
        fh.write("not a listing")
    raises(lambda: Export(export).list_tasks(), "a non-listing export raises SchedulerError",
           SchedulerError)
    check(Export(export).codec == ("oem" if os.name == "nt" else "utf-8"),
          "Export defaults to the same codec as Schtasks")

    # --- the command line, end to end ------------------------------------------
    def cli(argv, scheduler=None):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = main(argv, scheduler)
        return code, out.getvalue(), err.getvalue()

    def manifest_file(label):
        return os.path.join(tmp, label + ".json")

    def read_bytes(path):
        with open(path, "rb") as fh:
            return fh.read()

    def read_json(path):
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)

    NIGHTLY = ["--name", "^\\\\Nightly\\\\"]
    put_state(base)
    m1 = manifest_file("m1")
    code, out, err = cli(NIGHTLY + ["--manifest", m1], sched())
    check(code == 0 and "disable  \\Nightly\\Compress" in out and "disable  \\Nightly\\Load" in out
          and "leave    \\Nightly\\Reconcile" in out and "Add --apply" in out,
          "a plan lists what it would disable and what it would leave")
    check(not os.path.exists(m1) and enabled_of("\\Nightly\\Load") is True,
          "a plan without --apply writes no manifest and changes no task")

    code, out, err = cli(NIGHTLY + ["--manifest", m1, "--apply"], sched())
    saved = read_json(m1)
    check(code == 0 and "froze 2 of 2" in out, "--apply freezes and reports 2 of 2")
    check(enabled_of("\\Nightly\\Load") is False and enabled_of("\\Nightly\\Compress") is False,
          "the matched enabled tasks are now disabled")
    check(enabled_of("\\Other\\Backup") is True, "a task outside the selector is untouched")
    check(saved["disabled"] == ["\\Nightly\\Compress", "\\Nightly\\Load"] and saved["count"] == 2
          and saved["already_off"] == ["\\Nightly\\Reconcile"] and saved["thawed"] is None,
          "the manifest on disk records the two tasks and the one already off")
    check(not os.path.exists(m1 + ".tmp"), "no temporary file is left beside the manifest")
    first_bytes = read_bytes(m1)

    code, out, err = cli(NIGHTLY + ["--manifest", m1, "--apply"], sched())
    check(code == 3 and "already exists" in err and read_bytes(m1) == first_bytes,
          "a repeated freeze onto the same manifest exits 3 and leaves it byte for byte  "
          "<-- pinned defect")
    m2 = manifest_file("m2")
    code, out, err = cli(NIGHTLY + ["--manifest", m2, "--apply"], sched())
    check(code == 3 and "empty" in err and not os.path.exists(m2),
          "a repeated freeze onto a new path exits 3 and writes NO manifest  "
          "<-- pinned defect")
    check("canary" not in out + err + read_bytes(m1).decode("utf-8"),
          "no action argument reaches stdout, stderr or the manifest")

    code, out, err = cli(["--status", m1], sched())
    check(code == 0 and out.startswith("OFF") and "ok: 2 task(s)" in out,
          "--status on a live freeze exits 0")
    sched().set_enabled("\\Nightly\\Load", True)
    code, out, err = cli(["--status", m1], sched())
    check(code == 1 and "DRIFT" in out, "--status exits 1 when a frozen task is back on")
    sched().set_enabled("\\Nightly\\Load", False)

    code, out, err = cli(["--thaw", m1], sched())
    check(code == 0 and "enable   \\Nightly\\Load" in out and
          "left     \\Nightly\\Reconcile  (off before the freeze, stays off)" in out
          and "nothing changed" in out, "a thaw plan lists what it would enable and what stays off")
    check(enabled_of("\\Nightly\\Load") is False, "a thaw plan changes nothing")
    code, out, err = cli(["--thaw", m1, "--apply"], sched())
    check(code == 0 and "2 of 2 task(s) in the manifest are enabled" in out,
          "a thaw restores 2 of 2")
    check(enabled_of("\\Nightly\\Load") is True and enabled_of("\\Nightly\\Compress") is True,
          "the frozen tasks are enabled again")
    check(enabled_of("\\Nightly\\Reconcile") is False,
          "the task that was off BEFORE the freeze is still off after the thaw  "
          "<-- pinned defect")
    check(read_json(m1)["thawed"] is not None, "the manifest is marked thawed")
    sched().set_enabled("\\Nightly\\Load", False)
    code, out, err = cli(["--thaw", m1, "--apply"], sched())
    check(code == 3 and "already thawed" in err and enabled_of("\\Nightly\\Load") is False,
          "a second thaw is REFUSED and does not re-enable a task disabled since  "
          "<-- pinned defect")
    code, out, err = cli(["--status", m1], sched())
    check(code == 1 and "DRIFT" in out and "thawed at" in out,
          "--status on a thawed manifest reports a task disabled since as drift")

    # a freeze interrupted by a failure
    put_state(base, fail=["\\Nightly\\Compress"])
    m3 = manifest_file("m3")
    code, out, err = cli(NIGHTLY + ["--manifest", m3, "--apply"], sched())
    check(code == 1 and "froze 1 of 2" in out and "FAILED   \\Nightly\\Compress" in err,
          "a /change that fails exits 1 and says which task")
    saved = read_json(m3)
    check(saved["disabled"] == ["\\Nightly\\Load"] and saved["count"] == 1,
          "the manifest keeps only the task that really got disabled")
    put_state(base, fail=["\\Nightly\\Compress", "\\Nightly\\Load"])
    m4 = manifest_file("m4")
    code, out, err = cli(NIGHTLY + ["--manifest", m4, "--apply"], sched())
    check(code == 1 and not os.path.exists(m4) and "no manifest was kept" in err,
          "a freeze in which nothing could be disabled keeps no manifest  <-- pinned defect")
    put_state(base, ignore=["\\Nightly\\Load"])
    m5 = manifest_file("m5")
    code, out, err = cli(NIGHTLY + ["--manifest", m5, "--apply"], sched())
    check(code == 1 and "UNVERIFIED \\Nightly\\Load" in err
          and read_json(m5)["disabled"] == ["\\Nightly\\Compress"],
          "a /change that exits 0 and changes nothing is UNVERIFIED and is not recorded  "
          "<-- pinned defect")
    put_state(base, ignore=["\\Nightly\\Load", "\\Nightly\\Compress"])
    m6 = manifest_file("m6")
    code, out, err = cli(NIGHTLY + ["--manifest", m6, "--apply"], sched())
    check(code == 1 and not os.path.exists(m6),
          "when every change is ignored the manifest is removed, not kept empty")

    put_state(base, partial=["\\Nightly\\Load"])
    m5b = manifest_file("m5b")
    code, out, err = cli(NIGHTLY + ["--manifest", m5b, "--apply"], sched())
    check(code == 0 and "RECORDED \\Nightly\\Load" in err and "froze 2 of 2" in out
          and read_json(m5b)["disabled"] == ["\\Nightly\\Compress", "\\Nightly\\Load"],
          "a /change that errors after taking effect is recorded, so the thaw restores it  "
          "<-- pinned defect")
    put_state(get_state()["tasks"], partial=["\\Nightly\\Load"])
    code, out, err = cli(["--thaw", m5b, "--apply"], sched())
    check(code == 0 and "RECORDED \\Nightly\\Load" in err and "2 of 2 task(s)" in out
          and read_json(m5b)["thawed"] is not None,
          "a thaw /change that errors after taking effect counts as restored when it reads "
          "back on")

    # a thaw that cannot finish
    put_state(base)
    m7 = manifest_file("m7")
    cli(NIGHTLY + ["--manifest", m7, "--apply"], sched())
    put_state(get_state()["tasks"], fail=["\\Nightly\\Compress"])
    code, out, err = cli(["--thaw", m7, "--apply"], sched())
    check(code == 1 and "1 of 2 task(s)" in out and "FAILED   \\Nightly\\Compress" in err
          and read_json(m7)["thawed"] is None,
          "a thaw with one failure exits 1 and does not mark the manifest thawed")
    put_state(get_state()["tasks"])
    code, out, err = cli(["--thaw", m7, "--apply"], sched())
    check(code == 0 and "2 of 2 task(s)" in out and "1 already on" in out,
          "a re-run retries only what is still off")
    put_state(base)
    m8 = manifest_file("m8")
    cli(NIGHTLY + ["--manifest", m8, "--apply"], sched())
    put_state(get_state()["tasks"], ignore=["\\Nightly\\Load"])
    code, out, err = cli(["--thaw", m8, "--apply"], sched())
    check(code == 1 and "UNVERIFIED \\Nightly\\Load" in err,
          "a thaw whose /change is ignored is UNVERIFIED and exits 1")
    put_state(base)
    m9 = manifest_file("m9")
    cli(NIGHTLY + ["--manifest", m9, "--apply"], sched())
    put_state([t for t in get_state()["tasks"] if t["name"] != "\\Nightly\\Load"])
    code, out, err = cli(["--thaw", m9], sched())
    check(code == 1 and "MISSING  \\Nightly\\Load" in err,
          "a thaw plan with a deleted task exits 1")
    code, out, err = cli(["--thaw", m9, "--apply"], sched())
    check(code == 1 and "1 missing" in out and read_json(m9)["thawed"] is None,
          "a thaw with a deleted task enables the rest, exits 1 and is not marked thawed")

    # manifests that are not usable
    empty = manifest_file("empty")
    with open(empty, "w") as fh:
        json.dump(mutated(disabled=[], count=0), fh)
    put_state(base)
    code, out, err = cli(["--thaw", empty, "--apply"], sched())
    check(code == 3 and "0 of 0" not in out and "no task to restore" in err,
          "--thaw of an empty manifest exits 3, never '0 of 0 restored'  <-- pinned defect")
    code, out, err = cli(["--status", empty], sched())
    check(code == 3, "--status of an empty manifest exits 3")
    junk = manifest_file("junk")
    with open(junk, "w") as fh:
        fh.write("{not json")
    code, out, err = cli(["--thaw", junk], sched())
    check(code == 2 and "cannot read the manifest" in err, "a manifest that is not JSON exits 2")
    code, out, err = cli(["--status", os.path.join(tmp, "absent.json")], sched())
    check(code == 2, "a manifest that does not exist exits 2")

    # scheduler and file failures
    code, out, err = cli(NIGHTLY + ["--manifest", m1], Export(os.path.join(tmp, "x.xml")))
    check(code == 2 and "cannot read" in err, "an unreadable scheduler exits 2")
    code, out, err = cli(NIGHTLY + ["--manifest", os.path.join(tmp, "no", "dir", "m.json"),
                                    "--apply"], sched())
    check(code == 2 and "nothing was disabled" in err and enabled_of("\\Nightly\\Load") is True,
          "a manifest that cannot be created stops the freeze before any change")

    class Flaky(object):
        """Lists once, then fails: the read-back after the changes."""

        def __init__(self):
            self.inner = sched()
            self.calls = 0

        def list_tasks(self):
            self.calls += 1
            if self.calls > 1:
                raise SchedulerError("gone")
            return self.inner.list_tasks()

        def set_enabled(self, name, on):
            self.inner.set_enabled(name, on)

    put_state(base)
    m10 = manifest_file("m10")
    code, out, err = cli(NIGHTLY + ["--manifest", m10, "--apply"], Flaky())
    check(code == 2 and "cannot read the scheduler back" in err
          and read_json(m10)["count"] == 2,
          "if the read-back fails the manifest keeps every attempted task and exits 2")
    code, out, err = cli(["--thaw", m10, "--apply"], Flaky())
    check(code == 2 and "cannot read the scheduler back" in err,
          "a thaw whose read-back fails exits 2")

    class Locked(object):
        """Accepts every change, then lets nothing be written back."""

        def __init__(self, inner):
            self.inner = inner

        def list_tasks(self):
            return self.inner.list_tasks()

        def set_enabled(self, name, on):
            self.inner.set_enabled(name, on)

    real_replace = os.replace

    def broken_replace(src, dst):
        raise OSError("disk full")

    put_state(base, fail=["\\Nightly\\Compress"])
    m11 = manifest_file("m11")
    os.replace = broken_replace
    try:
        code, out, err = cli(NIGHTLY + ["--manifest", m11, "--apply"], Locked(sched()))
    finally:
        os.replace = real_replace
    check(code == 2 and "cannot trim the manifest" in err,
          "a manifest that cannot be trimmed exits 2 and says it lists every attempt")
    put_state(base)
    m12 = manifest_file("m12")
    cli(NIGHTLY + ["--manifest", m12, "--apply"], sched())
    os.replace = broken_replace
    try:
        code, out, err = cli(["--thaw", m12, "--apply"], sched())
    finally:
        os.replace = real_replace
    check(code == 2 and "could not be marked thawed" in err
          and enabled_of("\\Nightly\\Load") is True,
          "a thaw that cannot mark the manifest exits 2 after enabling the tasks")

    # --from-xml and the real-program path
    with open(export, "w", encoding="utf-8") as fh:
        fh.write(fixture_xml(base))
    code, out, err = cli(NIGHTLY + ["--manifest", manifest_file("m13"), "--from-xml", export,
                                    "--schtasks", "unused"])
    check(code == 0 and "disable  \\Nightly\\Load" in out,
          "--from-xml plans a freeze from a saved listing")
    code, out, err = cli(NIGHTLY + ["--manifest", manifest_file("m14"), "--from-xml", export,
                                    "--apply"])
    check(code == 64 and "cannot be combined" in err and not os.path.exists(manifest_file("m14")),
          "--from-xml with --apply is a usage error")
    code, out, err = cli(["--status", m3, "--from-xml", export])
    check(code == 1 and out.startswith("ON"), "--status can read a saved listing")
    code, out, err = cli(NIGHTLY + ["--manifest", manifest_file("m15"), "--schtasks",
                                    os.path.join(tmp, "absent.exe")])
    check(code == 2 and "cannot run" in err,
          "--schtasks names the program; a missing one exits 2 with its name")
    code, out, err = cli(NIGHTLY + ["--manifest", manifest_file("m16"), "--schtasks",
                                    sys.executable])
    check(code == 2, "a program that is not schtasks exits 2")

    # usage
    unused = manifest_file("unused")
    for argv, word, label in (
            ([], "nothing to do", "no mode"),
            (["--apply"], "nothing to do", "--apply alone"),
            (["--name", "x"], "--manifest", "--name without --manifest"),
            (["--manifest", unused], "--name or --action", "--manifest without a selector"),
            (["--except", "x", "--manifest", unused], "--name or --action",
             "--except without a selector"),
            (["--name", "x", "--manifest", unused, "--thaw", unused], "separate modes",
             "a freeze together with --thaw"),
            (["--thaw", unused, "--status", unused], "separate modes", "--thaw with --status"),
            (["--status", unused, "--apply"], "only reads", "--status with --apply"),
            (["--name", "", "--manifest", unused], "empty", "an empty --name"),
            (["--action", "(", "--manifest", unused], "regular expression",
             "a broken --action"),
            (["--name", "x", "--manifest", unused, "--except", " "], "empty",
             "a blank --except")):
        code, out, err = cli(argv, sched())
        check(code == 64 and word in err, "usage: %s exits 64" % label)
    for flag in ("--ap", "--app", "--appl"):
        code, out, err = cli(NIGHTLY + ["--manifest", manifest_file("abbr"), flag], sched())
        check(code == 64 and not os.path.exists(manifest_file("abbr")),
              "the prefix %s of --apply is REFUSED by the parser  <-- pinned defect" % flag)
    for flag in ("--man", "--th", "--stat", "--from", "--sch", "--sel", "--exc"):
        code, out, err = cli([flag, "x"], sched())
        check(code == 64, "the prefix %s is refused, not expanded" % flag)
    check(build_parser().allow_abbrev is False, "the parser has allow_abbrev False")
    code, out, err = cli(["--no-such-flag"], sched())
    check(code == 64 and "unrecognized" in err, "an unknown flag exits 64")
    code, out, err = cli(["--help"], sched())
    check(code == 0 and "--apply" in out and "Exit:" in out, "--help prints the flags and exits 0")
    # no environment variable and no config file can turn --apply on
    os.environ["TASKFREEZE_APPLY"] = "1"
    try:
        put_state(base)
        cli(NIGHTLY + ["--manifest", manifest_file("env")], sched())
    finally:
        os.environ.pop("TASKFREEZE_APPLY", None)
    check(enabled_of("\\Nightly\\Load") is True and not os.path.exists(manifest_file("env")),
          "no environment variable turns --apply on")

    # crashes and odd characters
    class Exploding(object):
        def list_tasks(self):
            raise RuntimeError("boom")

    code, out, err = cli(NIGHTLY + ["--manifest", unused], Exploding())
    check(code == 70 and "unexpected RuntimeError" in err and "Traceback" in err,
          "an unexpected exception exits 70, not the 1 that means 'not everything was done'  "
          "<-- pinned defect")
    check(main.__doc__ is not None and EXIT_CRASH == 70, "the crash code is 70")
    wrapped = io.TextIOWrapper(io.BytesIO(), encoding="ascii")
    _tolerate_odd_characters((wrapped,))
    print("\u00e9", file=wrapped, flush=True)
    check(wrapped.buffer.getvalue().strip() == b"?",
          "a console that cannot print a character prints ? and keeps going  <-- pinned defect")
    wrapped.close()
    check(_tolerate_odd_characters((wrapped, io.StringIO())) is None,
          "a closed or non-reconfigurable stream is tolerated")

    # the harness itself
    c2, r2, report2 = make_harness()
    with contextlib.redirect_stdout(io.StringIO()) as sink:
        c2(True, "ok")
        c2(False, "bad one")
        r2(lambda: None, "no error")
        r2(lambda: 1 / 0, "wrong error")
        r2(lambda: (_ for _ in ()).throw(ValueError("x")), "right error")
        code2 = report2()
    text = sink.getvalue()
    check(code2 == 1 and "FAIL  bad one" in text and "FAILED  no error" in text
          and "5 assertions, 3 failed" in text,
          "check() and raises() record failures, and the footer names them and exits 1"
          "  <-- pinned defect")
    c3, r3, report3 = make_harness()
    with contextlib.redirect_stdout(io.StringIO()) as sink:
        c3(True, "a")
        code3 = report3()
    check(code3 == 0 and "1 assertions, 0 failed" in sink.getvalue(),
          "an all-green harness exits 0 with the 0 failed footer")

    return report()


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
