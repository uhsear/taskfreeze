# taskfreeze

Disable a set of Windows scheduled tasks for a maintenance window, and later turn back on exactly
the ones it turned off. It refuses to write an empty record, it never enables a task that was
already off before the freeze, and it never reports a freeze while a task it disabled still runs.

The script everyone writes for a maintenance window looks like this:

```powershell
$tasks = Get-ScheduledTask -TaskPath '\Nightly\'
$tasks | Where-Object State -ne 'Disabled' | Export-Clixml freeze.xml     # "the tasks I changed"
$tasks | Disable-ScheduledTask
# ... patch the server ...
Import-Clixml freeze.xml | Enable-ScheduledTask
```

Three things go wrong, and all three show up after the fact.

- **The second freeze.** The window slips a day, someone runs the freeze again, and every task is
  already off. The script finds no enabled task and writes a record of zero tasks over the first
  record. The thaw prints "0 of 0 restored", exits 0, and the nightly jobs stay off. Nobody
  notices until the report that depends on them goes stale.
- **The blanket enable.** The thaw enables "everything in scope". One task in that folder was
  switched off on purpose a year ago, because the feed it reads was retired. The window turns it
  back on, and it runs against a source that no longer exists.
- **The task that was running.** The nightly load started at 01:50. The freeze ran at 02:00 and
  disabled it. Disabling a task does not stop the run in progress, so the load kept writing into
  the tables that the patch was about to change. The script printed "frozen" and the patch
  started underneath it.

This tool refuses all three. A freeze that has nothing to disable exits 3 and writes no file.
A freeze with a task that runs now exits 3 and changes nothing, unless you give `--wait SECONDS`.
With `--wait` it disables the tasks, waits up to that long for the running ones to end, and prints
`NOT FROZEN` with exit 1 if one still runs. A freeze never
overwrites a record that exists. A task is in the record only if the tool saw it enabled.
When the read-back works, only tasks that read back as disabled stay in it. The thaw touches only
the tasks in the record, so it never enables a task that was off before the freeze.
A record that was already thawed cannot be thawed again, because the second thaw could enable a
task that someone disabled in the meantime.

```
python taskfreeze.py --name "^[\\]Nightly[\\]" --manifest freeze1.json            # plan only
python taskfreeze.py --name "^[\\]Nightly[\\]" --manifest freeze1.json --apply    # freeze
python taskfreeze.py --name "^[\\]Nightly[\\]" --manifest freeze1.json --apply --wait 300
                                                                                  # freeze, wait up to 5 minutes for running tasks
python taskfreeze.py --status freeze1.json                                        # read only
python taskfreeze.py --thaw freeze1.json --apply                                  # restore
```

Nothing changes without `--apply`. Python 3.9 or later, standard library only. The scheduler part
runs on Windows. The self-test also runs on Linux, and `--from-xml` plans from a saved listing
there.

## What a real run looks like

This is a real session on Windows 11 (build 26100), run on 2026-11-10 against three scratch tasks
that the run created and deleted afterwards. Two were enabled (`a`, `b`). One (`pre-off`) was
disabled before the freeze. The pattern is `^[\\]taskfreeze-dev[\\]`. The timestamp in the last
thaw is UTC, so it reads 2026-11-11 for a run on the evening of 2026-11-10 local time.

Nothing is cut. Standard output and standard error are merged, as a pipe delivers them. Standard
error is unbuffered, so a `refused:` line can print before the standard output lines of the same
run. The only edits are that `taskfreeze` stands for `python taskfreeze.py`, and that the last
three lines come from `schtasks /query /fo csv /nh` after the thaw, filtered to the scratch folder.

```
$ taskfreeze --name ^[\\]taskfreeze-dev[\\] --manifest m.json
leave    \taskfreeze-dev\pre-off  (already disabled, stays as it is)
disable  \taskfreeze-dev\a
disable  \taskfreeze-dev\b
2 to disable, 1 already off and left alone. Manifest: m.json
nothing changed. Add --apply to disable them.
rc=0

$ taskfreeze --name ^[\\]taskfreeze-dev[\\] --manifest m.json --apply
leave    \taskfreeze-dev\pre-off  (already disabled, stays as it is)
disable  \taskfreeze-dev\a
disable  \taskfreeze-dev\b
2 to disable, 1 already off and left alone. Manifest: m.json
disabled \taskfreeze-dev\a
disabled \taskfreeze-dev\b
froze 2 of 2 task(s). Manifest: m.json
rc=0

$ taskfreeze --status m.json
OFF      \taskfreeze-dev\a
OFF      \taskfreeze-dev\b
left     \taskfreeze-dev\pre-off  (off before the freeze, off now)
ok: 2 task(s) in the manifest
rc=0

$ taskfreeze --name ^[\\]taskfreeze-dev[\\] --manifest m2.json --apply
refused: every matched task is already disabled, so the manifest would be empty and a thaw would restore nothing. A freeze may still be in force: check --status on its manifest.
leave    \taskfreeze-dev\a  (already disabled, stays as it is)
leave    \taskfreeze-dev\b  (already disabled, stays as it is)
leave    \taskfreeze-dev\pre-off  (already disabled, stays as it is)
matched 0 enabled and 3 already disabled task(s); nothing was changed.
rc=3

$ taskfreeze --name ^[\\]taskfreeze-dev[\\] --manifest m.json --apply
refused: the manifest file already exists. A second freeze must not overwrite the record of the first. Thaw it, or choose a new path.
leave    \taskfreeze-dev\a  (already disabled, stays as it is)
leave    \taskfreeze-dev\b  (already disabled, stays as it is)
leave    \taskfreeze-dev\pre-off  (already disabled, stays as it is)
matched 0 enabled and 3 already disabled task(s); nothing was changed.
rc=3

$ taskfreeze --name ^[\\]taskfreeze-dev[\\] --manifest m3.json --ap
usage: taskfreeze [-h] [--name NAME] [--action ACTION]
                  [--except EXCEPT_PATTERN] [--manifest MANIFEST]
                  [--thaw MANIFEST] [--status MANIFEST] [--from-xml FILE]
                  [--wait SECONDS] [--poll SECONDS] [--schtasks PROGRAM]
                  [--apply] [--self-test]
taskfreeze: error: unrecognized arguments: --ap
rc=64

$ taskfreeze --thaw m.json --apply
enable   \taskfreeze-dev\a
enable   \taskfreeze-dev\b
left     \taskfreeze-dev\pre-off  (off before the freeze, stays off)
enabled  \taskfreeze-dev\a
enabled  \taskfreeze-dev\b
2 of 2 task(s) in the manifest are enabled: 2 enabled now, 0 already on, 0 missing
rc=0

$ taskfreeze --thaw m.json --apply
refused: this manifest was already thawed at 2026-11-11T01:55:05Z. A second thaw could re-enable a task someone has disabled since.
rc=3

"\taskfreeze-dev\a","1/1/2031 11:59:00 PM","Ready"
"\taskfreeze-dev\b","1/1/2031 11:59:00 PM","Ready"
"\taskfreeze-dev\pre-off","N/A","Disabled"
```

After the thaw, `a` and `b` read `Ready` and `pre-off` still reads `Disabled`. The two refused
freezes left no `m2.json`, and only `m.json` was in the folder at the end. The `--status` call
between the freeze and the thaw printed `OFF` for `a` and `b`, a `left` line for `pre-off`, and
exited 0.

## Running tasks

Disabling a task does not stop a run that has started. On Windows 11 the Status of a task read
`Running` after `schtasks /change /disable` had succeeded, and kept reading `Running` until
`schtasks /end` ended the instance. A freeze that disables a running load and then reports
"frozen" lets the load keep writing while the patch starts. The tool reads the run state and
does one of three things.

1. **No `--wait`: refuse.** If a matched task runs now, the freeze exits 3 before it changes
   anything. That covers a task it would disable and a task that is already disabled, for example
   one an administrator disabled by hand while it ran. A plan without `--apply` exits 3 too, so a
   dry run shows the refusal.
2. **`--wait SECONDS`: disable, then wait.** The tool disables first, so no new instance can
   start, then checks every `--poll` seconds (default 5) until every task it froze is idle or the
   time is up. Waiting first would leave the tasks enabled, and a trigger could start a fresh run
   during the wait. The limit is 3600 seconds.
3. **Still running at the deadline: NOT FROZEN.** The tool exits 1, prints `NOT FROZEN` and prints
   no `froze` line. The tasks stay disabled and in the record, so a thaw restores them. A task
   that was already disabled and still runs is not in the record, so the line counts only the
   tasks the freeze disabled.
   `--status` shows each one as `RUNNING` and counts it as drift until it ends. The tool never
   ends an instance itself. `schtasks /end` does that, and killing a run in the middle of a write
   is a decision for the person at the keyboard.

A `froze` line means the scheduler reports no running instance. It cannot show a detached child
process that a task started and left behind.

The tool also checks after every freeze, even when nothing ran at the start, because a trigger
can fire between the plan and the disable. It never reads an unreadable run state as idle. A
status word it does not know, a task missing from the run listing, and a failed read all stop the
run: before any change they exit 3 (unknown word, missing task) or 2 (failed read), and after the
changes the freeze reports `NOT FROZEN` (exit 1) or exits 2.

This is a real session on the same machine, run on 2026-11-10 with the same merging of output as
above. `long` runs `ping -n 120`, so it is still running when the first wait ends. `short` runs
`ping -n 6` and ends inside its wait. `idle` and `pre-off` do nothing. After the first thaw the
driver disabled `long` by hand and excluded it with `--except`, so the second freeze involves
`short` only. The last two lines come from `schtasks /query /fo csv /nh` and from a count of
scratch tasks left after the driver deleted them.

```
$ taskfreeze --name ^[\\]taskfreeze-dev[\\] --manifest m.json
refused: 1 task(s) run now (\taskfreeze-dev\long). Disabling a task does not stop a running instance, so it would keep writing during the window. Add --wait SECONDS to wait for them, or end them with schtasks /end.
leave    \taskfreeze-dev\pre-off  (already disabled, stays as it is)
matched 2 enabled and 1 already disabled task(s); nothing was changed.
rc=3

$ taskfreeze --name ^[\\]taskfreeze-dev[\\] --manifest m.json --apply
refused: 1 task(s) run now (\taskfreeze-dev\long). Disabling a task does not stop a running instance, so it would keep writing during the window. Add --wait SECONDS to wait for them, or end them with schtasks /end.
leave    \taskfreeze-dev\pre-off  (already disabled, stays as it is)
matched 2 enabled and 1 already disabled task(s); nothing was changed.
rc=3

$ taskfreeze --name ^[\\]taskfreeze-dev[\\] --manifest m.json --wait 3 --poll 1
leave    \taskfreeze-dev\pre-off  (already disabled, stays as it is)
disable  \taskfreeze-dev\idle
disable  \taskfreeze-dev\long
running  \taskfreeze-dev\long  (an instance runs now; a disabled task keeps running)
1 running. After the change the tool waits up to 3 s, polling every 1 s, and reports NOT FROZEN if one still runs.
2 to disable, 1 already off and left alone. Manifest: m.json
nothing changed. Add --apply to disable them.
rc=0

$ taskfreeze --name ^[\\]taskfreeze-dev[\\] --manifest m.json --apply --wait 3 --poll 1
RUNNING  \taskfreeze-dev\long: disabled, but an instance still runs and keeps writing. End it with schtasks /end, then check --status.
leave    \taskfreeze-dev\pre-off  (already disabled, stays as it is)
disable  \taskfreeze-dev\idle
disable  \taskfreeze-dev\long
running  \taskfreeze-dev\long  (an instance runs now; a disabled task keeps running)
1 running. After the change the tool waits up to 3 s, polling every 1 s, and reports NOT FROZEN if one still runs.
2 to disable, 1 already off and left alone. Manifest: m.json
disabled \taskfreeze-dev\idle
disabled \taskfreeze-dev\long
NOT FROZEN: 2 of 2 task(s) disabled, but 1 still running and 0 could not be read. Manifest: m.json
rc=1

$ taskfreeze --status m.json
OFF      \taskfreeze-dev\idle
RUNNING  \taskfreeze-dev\long  <-- drift
left     \taskfreeze-dev\pre-off  (off before the freeze, off now)
DRIFT: 2 task(s) in the manifest
rc=1

$ taskfreeze --status m.json
OFF      \taskfreeze-dev\idle
OFF      \taskfreeze-dev\long
left     \taskfreeze-dev\pre-off  (off before the freeze, off now)
ok: 2 task(s) in the manifest
rc=0

$ taskfreeze --thaw m.json --apply
enable   \taskfreeze-dev\idle
enable   \taskfreeze-dev\long
left     \taskfreeze-dev\pre-off  (off before the freeze, stays off)
enabled  \taskfreeze-dev\idle
enabled  \taskfreeze-dev\long
2 of 2 task(s) in the manifest are enabled: 2 enabled now, 0 already on, 0 missing
rc=0

$ taskfreeze --name ^[\\]taskfreeze-dev[\\] --except long --manifest m2.json --apply --wait 30 --poll 1
leave    \taskfreeze-dev\pre-off  (already disabled, stays as it is)
disable  \taskfreeze-dev\idle
disable  \taskfreeze-dev\short
running  \taskfreeze-dev\short  (an instance runs now; a disabled task keeps running)
1 running. After the change the tool waits up to 30 s, polling every 1 s, and reports NOT FROZEN if one still runs.
2 to disable, 1 already off and left alone. Manifest: m2.json
disabled \taskfreeze-dev\idle
disabled \taskfreeze-dev\short
finished \taskfreeze-dev\short
froze 2 of 2 task(s). Manifest: m2.json
rc=0

$ taskfreeze --thaw m2.json --apply
enable   \taskfreeze-dev\idle
enable   \taskfreeze-dev\short
left     \taskfreeze-dev\pre-off  (off before the freeze, stays off)
enabled  \taskfreeze-dev\idle
enabled  \taskfreeze-dev\short
2 of 2 task(s) in the manifest are enabled: 2 enabled now, 0 already on, 0 missing
rc=0

after, from schtasks /query /fo csv /nh:
"\taskfreeze-dev\idle","1/1/2031 11:59:00 PM","Ready"
"\taskfreeze-dev\long","N/A","Disabled"
"\taskfreeze-dev\pre-off","N/A","Disabled"
"\taskfreeze-dev\short","1/1/2031 11:59:00 PM","Ready"
leftover scratch tasks: 0
```

Every step in that session ran on real `schtasks`. The self-test runs the same cases against a stand-in:

| Claim | Real Windows | Self-test |
|---|---|---|
| A running task with no `--wait` refuses the freeze, exit 3, nothing changed | session, steps 1 and 2 | yes |
| A plan with `--wait` lists the running task and changes nothing | session, step 3 | yes |
| A task that ends inside the wait gives `froze` and exit 0 | session, `short` | yes |
| A task still running at the deadline gives `NOT FROZEN`, exit 1, record kept | session, `long` | yes |
| `--status` shows a disabled task that still runs as `RUNNING` drift, and is clean after it ends | session | yes |
| A task that starts between the plan and the disable gives `NOT FROZEN` | not run: the window cannot be hit on demand | yes, `start_on_change` |
| An unknown status word or a missing row refuses the freeze, exit 3 | not run: no unknown word appeared | yes |
| An unreadable run state exits 2 before any change, and `--thaw` does not need it | not run | yes |
| A task with one row per trigger is read by its busiest row | observed: 280 rows for 203 tasks, no differing status | yes |
| A disabled task keeps `Running` until its instance ends | session | modelled by the stand-in only |
| A task that was already disabled and still runs refuses the freeze (exit 3), and is `NOT FROZEN` at the deadline with `--wait` | second session | yes |
| `--status` shows a task that was off before the freeze but still runs as `RUNNING` drift | second session | yes |
| A task in the `Queued` state counts as running | not run: no queued task was made | yes, as a word |

A second real session, run on 2026-11-10 the same way, covers the case that the first one does not:
a task that was disabled by hand while its instance ran. `off-running` runs `ping -n 120`, was
started, and was then disabled with `schtasks /change /disable`, so it reads `Running` and is
disabled. `idle` does nothing. The driver ended the instance before the thaw.

```
$ taskfreeze --name ^[\\]taskfreeze-dev[\\] --manifest c.json --apply
refused: 1 task(s) run now (\taskfreeze-dev\off-running). Disabling a task does not stop a running instance, so it would keep writing during the window. Add --wait SECONDS to wait for them, or end them with schtasks /end.
leave    \taskfreeze-dev\off-running  (already disabled, stays as it is)
matched 1 enabled and 1 already disabled task(s); nothing was changed.
rc=3

$ taskfreeze --name ^[\\]taskfreeze-dev[\\] --manifest c.json --apply --wait 2 --poll 1
RUNNING  \taskfreeze-dev\off-running: disabled, but an instance still runs and keeps writing. End it with schtasks /end, then check --status.
leave    \taskfreeze-dev\off-running  (already disabled, stays as it is)
disable  \taskfreeze-dev\idle
running  \taskfreeze-dev\off-running  (an instance runs now; a disabled task keeps running)
1 running. After the change the tool waits up to 2 s, polling every 1 s, and reports NOT FROZEN if one still runs.
1 to disable, 1 already off and left alone. Manifest: c.json
disabled \taskfreeze-dev\idle
NOT FROZEN: 1 of 1 task(s) disabled, but 1 still running and 0 could not be read. Manifest: c.json
rc=1

$ taskfreeze --status c.json
OFF      \taskfreeze-dev\idle
RUNNING  \taskfreeze-dev\off-running  (off before the freeze, but an instance runs now)  <-- drift
DRIFT: 1 task(s) in the manifest
rc=1

$ taskfreeze --thaw c.json --apply
enable   \taskfreeze-dev\idle
left     \taskfreeze-dev\off-running  (off before the freeze, stays off)
enabled  \taskfreeze-dev\idle
1 of 1 task(s) in the manifest are enabled: 1 enabled now, 0 already on, 0 missing
rc=0

leftover scratch tasks: 0
```

The `NOT FROZEN` line counts the tasks the freeze disabled, so here it reads 1 of 1 while the
running task is the one that was already off.

How the run state is read, and what Microsoft documents about it, is in the next sections.

## What already exists

Windows already ships the switch. This tool adds the record and the refusals around it.

- **`Disable-ScheduledTask` and `Enable-ScheduledTask`.** The PowerShell cmdlets disable and enable
  a task. The reference page for `Disable-ScheduledTask` says "The `Disable-ScheduledTask` cmdlet
  disables a scheduled task" and shows no record of the state it found
  ([Disable-ScheduledTask](https://learn.microsoft.com/en-us/powershell/module/scheduledtasks/disable-scheduledtask)).
  They are the right tool for one task. The record and its checks are yours to write.
- **`schtasks /change /tn NAME /disable`.** The command-line form. The page lists `/ENABLE` and
  `/DISABLE` and says that to change all tasks on the local computer you must be a member of the
  Administrators group
  ([schtasks change](https://learn.microsoft.com/en-us/windows-server/administration/windows-commands/schtasks-change)).
  This tool calls exactly this command.
- **`schtasks /end` and `Stop-ScheduledTask`.** They end a running instance. The `schtasks end`
  page says it "stops only the instances of a program started by a scheduled task"
  ([schtasks end](https://learn.microsoft.com/en-us/windows-server/administration/windows-commands/schtasks-end)).
  The `Stop-ScheduledTask` page says it "immediately stops all running instances of a registered
  background task"
  ([Stop-ScheduledTask](https://learn.microsoft.com/en-us/powershell/module/scheduledtasks/stop-scheduledtask)).
  Both are the right tool when you decide a run may be cut off. The `Disable-ScheduledTask` page
  says nothing about a running instance, which is the gap this tool closes by waiting or refusing.
  This tool never calls either one.
- **Other GitHub projects.** `gh search repos` on 2026-10-23, before this repository was
  published, for `taskfreeze`, `freeze scheduled tasks`, `disable scheduled tasks restore
  manifest`, `task scheduler freeze thaw`, `schtasks disable enable` and `scheduled task
  maintenance window pause resume` returned empty lists. The same search for `taskfreeze` now
  returns this repository only. A search for `disable-scheduledtask` found
  `SkylineCommunications/SLC-AS-Enable-Disable-ScheduledTasks`, a C# repository with no
  description and a readme that holds only its title, so this README cannot say what it does. That shows only that the search found
  nothing. It does not show that nothing exists. On 2026-11-10 `gh search repos` for `freeze
  scheduled tasks running`, `disable scheduled tasks wait running` and `schtasks disable wait
  running task` returned empty lists.

## Requirements

- Python 3.9 or later. Standard library only. No credentials, no network.
- For `--apply` on Windows: `schtasks.exe` on the path and the rights `/change` needs (see above).
- `--from-xml` reads a saved listing, so a plan can run on any machine, including Linux.

## How a task is read

The tool runs `schtasks /query /xml`. The page for the command says: "Outputs all task definitions on the system to XML
format"
([schtasks query](https://learn.microsoft.com/en-us/windows-server/administration/windows-commands/schtasks-query)).
Three details decide whether a task counts as enabled.

- **Only `Settings/Enabled` counts.** The element is a boolean with `default="true"`
  ([Enabled, settingsType](https://learn.microsoft.com/en-us/windows/win32/taskschd/taskschedulerschema-enabled-settingstype-element)).
  A trigger has its own `Enabled` element. Read that one by mistake and a disabled task looks
  live. The self-test pins this.
- **A missing element means enabled.** The schema default applies to an empty element, not to an
  absent one, so the schema alone does not settle this. The basis is a live check: on the Windows
  11 machine used here, `schtasks /query /xml` left the element out of every task that was
  enabled.
- **The value must be a legal `xs:boolean`.** The lexical space is `true`, `false`, `1` and `0`, and
  white space is collapsed ([XML Schema 1.1 Part 2, section 3.3.2](https://www.w3.org/TR/xmlschema11-2/#boolean)).
  Anything else, including `FALSE`, stops the run with an error. A guess could read a disabled task
  as enabled.

The listing is refused, not partly read, when it holds no task, repeats a task path, or holds a
definition with no path comment above it. A task the selectors cannot see would never be frozen.

## How the run state is read

The tool runs `schtasks /query /fo csv /nh` as a second call. The query page documents `/fo` with
`CSV` and says `/nh` "removes column headings from the table display" and is valid with the
`TABLE` or `CSV` formats
([schtasks query](https://learn.microsoft.com/en-us/windows-server/administration/windows-commands/schtasks-query)).
The page does not describe the columns. The three fields are the task path, the next run time
and the Status, and that order was observed on Windows 11, not read from the documentation.

- **The states.** The `TASK_STATE` enumeration has five members: Unknown, Disabled, Queued, Ready
  and Running. Queued means "instances of the task are queued", and Running means "one or more
  instances of the task is running"
  ([TASK_STATE](https://learn.microsoft.com/en-us/windows/win32/api/taskschd/ne-taskschd-task_state)).
  The tool counts Running and Queued as busy, Ready and Disabled as idle, and every other word as
  unknown.
- **The words.** On the test machine the Status column held `Ready`, `Disabled` and `Running`. No
  other word appeared, so the words `Queued` and `Could not start` are not confirmed by an
  observation. Only the English words are known. The tool does not translate.
- **One row per trigger.** The same query printed 280 rows for 203 tasks, because a task with
  several triggers has one row for each. The tool keeps the worst word of the rows, busy over
  unknown over idle.
- **Not atomic.** The run state is a second call, made after the task listing. A task can start in
  between. The check after the disable closes that gap for the tasks that were frozen.

## Usage

A freeze selects tasks with `--name`, `--action`, or both. At least one is required: with neither,
the tool would select every task. `--except` removes tasks. All three are case-insensitive regular
expressions, because Windows task names are. An empty pattern is a usage error.

A task path starts with a backslash, and Windows command-line parsing halves a run of backslashes
before a closing quote. Write `[\\]` for one backslash, as in the examples. A pattern that ends in a
lone backslash gets that hint in its error message.

| Flag | Meaning |
|---|---|
| `--name RE` | Regex on the task path, for example `^[\\]Nightly[\\]`. |
| `--action RE` | Regex on the task's command and arguments. |
| `--except RE` | Tasks whose path matches are never selected. |
| `--manifest FILE` | Where a freeze writes its record. It must not exist. |
| `--thaw FILE` | Turn the tasks in this record back on. |
| `--status FILE` | Compare the scheduler with this record. Read only. |
| `--from-xml FILE` | Read a saved `schtasks /query /xml` listing. Read only: it cannot be combined with `--apply`. |
| `--wait SECONDS` | A freeze waits up to this long, 0 to 3600, for running tasks to end. 0 means no wait. Without it a running task refuses the freeze. Freeze only. |
| `--poll SECONDS` | Seconds between run-state checks while waiting, 0.05 to 60. Default 5. Freeze only. |
| `--schtasks PROGRAM` | The scheduler program. Default `schtasks`. |
| `--apply` | Change the scheduler and write the record. Without it, nothing is written. |
| `--self-test` | Run the offline assertions and exit. |

The parser has `allow_abbrev=False`. A prefix such as `--ap` is refused with exit 64 instead of
running as `--apply`. The self-test pins `--ap`, `--app` and `--appl`, and one prefix of every
other long flag: `--na`, `--act`, `--exc`, `--man`, `--th`, `--stat`, `--from`, `--sch`, `--sel`,
`--he`, `--wa` and `--po`.

Exit codes:

| Code | Meaning |
|---|---|
| 0 | Done, or a plan with nothing wrong. |
| 1 | Not everything was done or verified: a task failed to change, did not read back as changed, still runs after the wait, or is missing. `--status` also exits 1 on drift, including a disabled task that still runs. A freeze plan or a `--status` that reads a saved listing with `--from-xml` also exits 1, because that holds no run state. A `--thaw` plan from a saved listing reads no run state and exits 0. |
| 2 | The scheduler, the run state or a file could not be read or written. |
| 3 | Refused: nothing to freeze, a record already exists, the record is empty or spent, a task runs and there is no `--wait`, or the run state of a task cannot be told. |
| 64 | Usage error. |
| 70 | An unexpected crash in the tool. |

### What the record holds

A JSON file with `disabled` (tasks this freeze turned off), `already_off` (matched tasks that were
off before, left alone), the three patterns, a UTC timestamp, a `count` and a `thawed` time that is
`null` until a thaw finishes. The thaw checks the record before it touches anything. It refuses a
record with an empty `disabled` list, a `count` that disagrees with the list, a task in both lists,
a repeated task, a task path with no leading backslash, or an unknown `version`.

### Order of events in a freeze

1. Read the scheduler and the run state, then plan. Refuse if the record exists, nothing matched,
   a matched name lost a character to the console code page, no matched task is enabled, the run
   state of any matched task, enabled or already off, cannot be told, or one runs and there is no
   `--wait`.
2. Create the record with mode `x`, which fails if the file exists. It is written before the first
   change, so a crash half way never leaves a disabled task with no record.
3. Disable each task, then read the scheduler back.
4. Keep in the record only the tasks that read back as disabled. A `/change` that reported an
   error but took effect is kept, so the thaw restores it. A `/change` that reported success but
   changed nothing is dropped and reported as `UNVERIFIED`. If nothing was disabled, the record is
   removed and the exit code is 1. If the read-back itself fails, the tool cannot tell which
   changes took effect. It keeps every attempted task, prints the reason and exits 2. Every kept
   task was enabled before the freeze, so a later thaw can only enable a task that was already
   live. `--status` shows which of them are still enabled.

5. Read the run state of the tasks the freeze disabled, the ones whose change failed and the ones
   that were already off, waiting up to `--wait` for the running ones. If one still runs or cannot be read, print `NOT FROZEN` and exit 1. If the run
   state cannot be read at all, exit 2. The record stays in both cases.

The thaw reads back the same way. It does not read the run state. It marks the record thawed only when every listed task is
enabled, so a re-run after a partial thaw retries the rest.

## Self-test

`python taskfreeze.py --self-test` runs offline. It builds listings in the shape `schtasks` prints,
and it drives the real subprocess path through a stand-in `schtasks` program that keeps its tasks
in a JSON file. It can be told to fail, to ignore a change, to change and then error, to hang, to
report a task as running for a set number of listings, to start a task when it is disabled, to
report an odd status word, to repeat or drop a row, and to fail the run-state query.

```
taskfreeze self-test: temporary files and a stand-in schtasks, no network
--------------------------------------------------------------------
...
PASS  a disabled task whose triggers all say Enabled true is still disabled  <-- pinned defect
PASS  an ENABLED task whose trigger says Enabled false is still enabled, so a freeze does not skip it as already off  <-- pinned defect
...
PASS  an empty listing is refused, not read as zero tasks  <-- pinned defect
...
PASS  --action reads the command text, never the task path  <-- pinned defect
...
PASS  a freeze whose matches are all already off is REFUSED, because its manifest would be empty  <-- pinned defect
PASS  a freeze onto a manifest that exists is REFUSED  <-- pinned defect
...
PASS  a task with one row per trigger is busy when a later row says Running  <-- pinned defect
...
PASS  a freeze whose task runs is REFUSED without --wait
PASS  a freeze is REFUSED when a task that was already disabled still runs  <-- pinned defect
...
PASS  --status flags a task that was already off before the freeze but still runs  <-- pinned defect
...
PASS  settle stops on the deadline, cuts the last sleep short and still reports the task busy  <-- pinned defect
...
PASS  a failing scheduler with only stdout never has that stdout echoed  <-- pinned defect
...
PASS  the task that was off BEFORE the freeze is still off after the thaw  <-- pinned defect
...
PASS  a second thaw is REFUSED and does not re-enable a task disabled since  <-- pinned defect
...
PASS  a freeze with a running task and no --wait is REFUSED and changes nothing  <-- pinned defect
...
PASS  a task still running when the wait ends is NOT FROZEN, exits 1 and never prints 'froze'  <-- pinned defect
...
PASS  --status reports a disabled task that still runs as drift  <-- pinned defect
...
PASS  a task that starts between the plan and the disable is caught by the final check  <-- pinned defect
...
PASS  the prefix --ap of --apply is REFUSED by the parser  <-- pinned defect
...
PASS  check() and raises() record failures, and the footer names them and exits 1  <-- pinned defect
PASS  an all-green harness exits 0 with the 0 failed footer
--------------------------------------------------------------------
275 assertions, 0 failed
```

The full run prints all 275 assertions. Each `...` line is where this block is cut.

| Host | Python | Assertions |
|---|---|---|
| Windows 11 | 3.13.2 | 275, 0 failed |
| Windows 11 | 3.12.10 | 275, 0 failed |
| Windows 11 | 3.9.25 | 275, 0 failed |
| Ubuntu | 3.12.3 | 275, 0 failed |

The counts are equal because no assertion depends on the host. The default codec assertion checks
`oem` on Windows and `utf-8` elsewhere in one assertion that passes on both. The run-state cases
use a stand-in written in Python, so they do not need `schtasks` on Linux either.

Branch coverage, measured on Windows with `coverage run --branch taskfreeze.py --self-test`: 1258
statements, 276 branches, 0 missed, 100 percent, with no `pragma` and no coverage configuration.
The `__main__` guard is covered by running the file through `runpy`, once as `__main__` and once
under another name. `coverage` is not installed on the Linux host, so Linux coverage was not
measured. `pyflakes` is clean on Windows, and `python -W error` compiles the file on both hosts.
`pyflakes` is not installed on the Linux host.

### Do the assertions fail when the logic is wrong

A passing self-test says little if it cannot go red. Each of these edits to a copy of the script made
the self-test fail:

| Edit | Failing assertions |
|---|---|
| Drop the refusal when no matched task is enabled | 3 |
| Drop the "already thawed" refusal | 3 |
| Let the thaw also walk `already_off` | 16 |
| Set `allow_abbrev=True` | 7 |
| Drop the empty-`disabled` check in manifest validation | 4 |
| Read any `Enabled` element in a task, not only `Settings/Enabled` | 1 |
| Match `--action` against the task path | 1 |
| Let only the last frozen task decide drift in `--status` | 1 |
| Open the record with mode `w` instead of `x` | 2 |
| Write the record in place, with no temporary file | 1 |
| Stamp local time instead of UTC | 1 |
| Read an unknown status word as idle | 18 |
| Keep the first row of a repeated task, not the busiest | 2 |
| Do not refuse a freeze when a task runs | 10 |
| Skip the final run check after the disable | 6 |
| Check only the tasks that read back disabled, not every matched task | 2 |
| Ignore the run state in `--status`, by editing the ternary in `status_report` | 4 |
| Read a task missing from the run listing as idle | 6 |
| Do not cut the last sleep short at the deadline | 1 |
| Print `froze` on the `NOT FROZEN` line | 5 |
| Report a task whose disable failed as finished | 1 |
| Show the scheduler's stdout in an error when stderr is empty | 1 |

All 22 rows were measured against the committed script, which holds 275 assertions.

## Why the obvious version is wrong

The obvious freeze marks "tasks I changed" as "tasks that matched". The first run is fine. The
second run matches the same tasks, finds them off, and either records them again (the thaw then
enables tasks that the first freeze owns) or records nothing (the thaw restores nothing). This tool
records a task only if it changed it, and it says why when that set is empty.

The obvious thaw reads the exit status of `schtasks /change`. An exit of 0 does not prove the task
changed, and an exit of 1 does not prove it did not. The tool reads the scheduler back and trusts
that. The self-test has a stand-in that does each of the four things, and one assertion per case.

The obvious freeze also reads "disabled" as "stopped". It is not: a run in progress goes on. This
tool reads the run state after the disable and says `NOT FROZEN` while one remains.

## What it will not do

- It does not stop a task that is running. This was checked again on Windows 11 on 2026-11-10: a
  task started with `schtasks /run` read `Running`, `schtasks /change /disable` succeeded, and
  the task still read `Running` until `schtasks /end` ended it. The freeze refuses, waits, or
  reports `NOT FROZEN` instead (see Running tasks). End a running instance yourself with
  `schtasks /end`.
- It does not work on a remote computer. `schtasks` has `/s`; this tool does not pass it.
- It does not touch the tasks it did not select, and it does not delete a task.
- It does not follow a task that someone renamed between the freeze and the thaw. The thaw reports
  it `MISSING` and exits 1.
- It cannot see a task a group policy or another administrator re-enables during the window. Run
  `--status` before the thaw to see that.

## Limits

- Every line and branch of the script ran in the self-test, and no line is excluded from the
  coverage figure. The subprocess calls in the self-test go to a stand-in program, not to the real
  `schtasks`. The real `schtasks` was exercised by the session above, which is not part of the
  self-test because it creates scheduled tasks.
- The codec for `schtasks` output is the OEM code page on Windows. A name the console cannot carry
  comes back with `?` or U+FFFD, and the tool refuses to freeze such a name, because a `/change` on
  it would address a different task or none.
- The record is a plain file. Anyone who can write it can change what the thaw does. The tool
  validates the record but does not sign it.
- The self-test covers the network-free design only. The tool has no network path.
- The error text of a failed scheduler call is its stderr only. Its stdout is never shown, because
  a listing holds every task argument. When stderr is empty the error says `no error text`.
- The record keeps your patterns as typed, so never put a secret in one.
- Run it from a directory you trust, or pass `--schtasks` with a full path. The default is the
  bare name `schtasks`, and Windows searches the current directory when it resolves a bare name.
- Run it from cmd or PowerShell. A POSIX shell, including Git Bash, halves the backslashes inside
  double quotes, and Git Bash on Windows halves them in single quotes too when it starts a native
  program. `'^[\\]Nightly[\\]'` reaches the tool as `^[\]Nightly[\]`, fails to compile and the tool
  exits 64. Write `\x5c` for a backslash instead, as in `'^\x5cNightly\x5c'`: the regex engine reads
  it as a backslash and the shell has nothing to halve.
- A task name with characters outside the OEM code page may be listed under a best-fit ASCII name,
  and the freeze then reports that task FAILED.
- Several tasks in one folder with names outside the OEM code page can make the listing look like
  repeated names, and the tool then refuses to run.
- A freeze refuses the whole run if any matched name holds a character the console code page
  cannot carry. Narrow the pattern or use `--except` for that task.
- Task names match by simple lower-casing, not by full Unicode case folding.
- `--action` sees only program actions. A task with a COM handler, an email or a message action
  has empty action text and is never selected by `--action`.
- A non-elevated run listed the same tasks as `schtasks /query /fo csv` by name on the test
  machine. Whether an unelevated run hides other users' tasks was not verified.
- A `--name` pattern that backtracks without limit can hang the tool.
- A dry run does not check that the manifest path can be created. The `--apply` run then exits 2
  before any change.
- A manifest that could not be fully written must be deleted by hand before the freeze is
  retried. No task is changed in that case.
- The trim and thaw steps write the record path plus `.tmp` beside the record (`freeze1.json.tmp`
  for `freeze1.json`) and replace any file with that name. A crash between the write and the rename leaves that file behind.
- The self-test checks that the exclusive create refuses an existing file. It does not start two
  freezes at the same instant on one path.
- Thaw every manifest you wrote, newest first. A task frozen by an earlier manifest is listed as
  already off in a later one, and the tool does not warn that another live freeze exists.
- A thaw enables every task in the manifest, even one an administrator disabled again during the
  window. Only a second thaw of the same manifest is refused.
- After a partial thaw, a task disabled on purpose before the re-run is enabled again.
- If another administrator disables a task between the plan and the read-back, a failed `/change`
  for that task is recorded and a later thaw enables it.
- If the tool is killed in the middle of a freeze, the record lists every task it meant to
  disable, including ones it never reached. The thaw reports those as already enabled, which is
  harmless. Ctrl+C is not caught: the process ends with a traceback instead of exit code 70.
- Exit 1 means "a change failed", "drift found" and "a task still runs".
- Save the record as UTF-8 without a byte-order mark. A record with one fails to parse and the
  tool exits 2.
- A hand-edited record with `true` or `1.0` in place of `1` is accepted for `version` and `count`.
- `--from-xml` reads a listing that has no byte-order mark with the OEM code page on Windows, so a
  UTF-8 file saved without a BOM shows garbled task names. The plan is read only.
- The self-test takes 39 seconds on a quiet Windows machine and about 17 seconds on Linux, because it waits for a stand-in scheduler to time out,
  and it sleeps about a second more in the wait cases. A Windows machine with many other Python processes running took 77 seconds.
- The self-test runs only from a file named `taskfreeze.py`, because the stand-in scheduler
  imports it by that name. A renamed copy exits 70 with `ModuleNotFoundError`.
- A task name with leading or trailing spaces is addressed without them, so the change fails or
  hits a different task. The freeze reports that task FAILED.
- When the console code page differs from the OEM page, for example after `chcp 65001`, `schtasks`
  writes names in the console page and the tool decodes them with the OEM page. A name such as
  `cafe` with an accent then reads as mojibake. The freeze reports it FAILED and a thaw or status
  reports it MISSING. No task is wrongly disabled or enabled.
- A `Settings/Enabled` element that has a child element is reported as `None` instead of its
  content. The run is refused, so only the message is poor.
- A task disabled by someone else between the plan and the change is recorded as frozen by this
  tool, and the thaw then enables it.
- If the tool is killed after the record is written but before the changes, and an administrator
  then disables a listed task on purpose, the thaw enables it.
- A freeze that overlaps an earlier live freeze gets no warning unless every matched task is
  already off. The second record then lists the first freeze's tasks as left alone.
- The `/s` behaviour and the Administrators requirement were not re-run for this README. The
  `schtasks /run` and `/end` behaviour was re-run on 2026-11-10.
- The run state is read as English words. On a Windows with another display language the words
  differ, the tool reads them as unknown, and a freeze is refused with exit 3 instead of guessed.
  This was not tried on a non-English machine.
- `Queued` counts as running, because the `TASK_STATE` page says instances are queued. No queued
  task was made on a real host, so the word `Queued` itself was not observed.
- Microsoft does not document the Status column of `schtasks /query /fo csv`, so the field order
  and the words rest on what Windows 11 printed. A later build could change them. The tool then
  refuses or reports `NOT FROZEN`, and does not report a clean freeze.
- The run state is a poll. The tool reports the state at the moment of the last read. A task
  that another administrator or a group policy re-enables afterwards can start again, as with
  the rest of the freeze. Run `--status` before the work.
- The wait ends at the deadline with the tasks disabled. Nothing re-enables them, so a
  `NOT FROZEN` run still needs a `--thaw` or a repeat of the freeze after the instance ends. A
  repeat onto the same record path is refused, so use `--status` and then `--thaw`.
- A task whose disable failed, or read back unchanged, is reported `FAILED` or `UNVERIFIED`. It
  is still checked for a running instance, and `--wait` applies to it. If it runs when the wait
  ends, the run prints `RUNNING ... NOT disabled` and the `NOT FROZEN` line with exit 1. A partial
  freeze with no running task prints `froze N of M` with exit 1.
- `UNVERIFIED ... still reads enabled` is also printed when the task no longer exists, because the
  read-back treats a missing task as not disabled. The exit code is non-zero either way.
- Step 1 of the freeze order lists the refusals. The code also refuses when a matched task that
  was already off has a run-state word the tool does not know, for example
  `the run state of 1 task(s) could not be told`.
- A malformed `Settings/Enabled` value or a non-path comment in a `--from-xml` listing is refused
  with exit 2, and the error quotes that text as it is. Task arguments are never echoed.
- Three Windows facts rest on observation, not on a Microsoft page: the backslash halving inside
  quoted command lines, the current-directory search for a bare `schtasks` name, and the OEM code
  page for `schtasks` output.
- The Microsoft `TASK_STATE` page says `TASK_STATE_DISABLED` means "no instances of the task are
  queued or running". This README's central observation is the opposite: a disabled task still
  read `Running` on Windows 11 on 2026-11-10. The page disagrees with the observation, and the
  tool rests on what was observed, not on the page.
- Each poll lists every task on the machine. A machine with a very large scheduler makes each
  poll slow, and `--poll` below the time one listing takes has no effect. This was not measured.
- `--from-xml` holds no run state. A freeze plan from it says `UNVERIFIED (run state not checked)` and exits 1,
  because the tool cannot tell whether a matched task runs, and exit 0 would claim that none does.
  `--status` from it says the same and exits 1. A plan from a saved listing cannot be applied:
  it tells you to run again without `--from-xml`. A `--thaw` plan from a saved listing reads no
  run state, prints no `UNVERIFIED` line and exits 0.
- The self-test writes nothing beside the script: it runs the stand-in with bytecode off and asserts that no `__pycache__` appears.
- `--wait 0` is the same as leaving `--wait` out: a running task refuses the freeze.
- The wait ends at the deadline plus at most one slow `schtasks` call, and a call that hangs is
  cut off after 120 seconds. A hung scheduler can therefore overrun `--wait` by about two minutes.
- Where the running task was already off before the freeze, the `NOT FROZEN` line counts only the
  tasks the freeze disabled, so it can read, for example, 2 of 2 task(s) disabled, 1 still
  running, with the running task not among the 2.
- `froze` means the scheduler reports no running instance. A task that starts a detached child
  process and exits reads `Ready` while the child still writes, and the tool cannot see it.
- `--status` and a freeze plan with `--from-xml` exit 1 even when the enabled flags match, for the reason above.
  Read the printed task lines for the enabled flags, and run `--status` on the machine for the
  run state.
- `--from-xml` is read as trusted input. A crafted listing could use a lot of memory.
- A closed output pipe during `--apply`, for example `| head -1`, ends the run with exit 70 and a
  traceback after some tasks are disabled. The record lists every task attempted, so run
  `--status`, then thaw or repeat the freeze.
- A record left from a thawed freeze still makes a new freeze onto the same path exit 3 with
  `the manifest file already exists`. Thawing does not free the path, so choose a new path.
- A task path or the `thawed` text in a record is printed as stored, so a record edited by someone
  else can send control characters to your terminal. Read a record you did not write before you
  run it.
- If another program locks the record when a freeze that disabled nothing tries to remove it, the
  tool exits 70 and leaves the file. Delete it by hand before you retry.
- A status word the tool does not know, such as `Could not start` or a word in another language,
  refuses the whole freeze with exit 3, and `--wait` does not help. Give that task to `--except`.
- Exit 0 on a dry run means the plan found no refusal. It does not mean that no task runs: a plan
  with `--wait` and a running task, without `--apply`, exits 0 and prints `nothing changed`. Read
  the printed run-state lines. The real run prints `NOT FROZEN` if the task keeps running.
- A `--thaw` of a record from a `NOT FROZEN` freeze enables the tasks again without checking
  that their instance has ended, because a thaw reads no run state.
- When every matched task is already disabled, the refusal does not say whether one of them still
  runs. Run `--status` on the record of the earlier freeze.
- A status row with a field longer than 131072 characters ends the run with exit 70 and a
  traceback, not exit 2.
- If the record cannot be rewritten after a partial freeze, the run exits 2 before the final
  running-task check, so it does not report a task that still runs.
- `schtasks` is started with the console as its input. If it asks a question, for example for a
  password, it reads your keyboard and the call ends at the 120 second limit with exit 2. The tool
  never sees or stores what you type.
- A `--from-xml` listing with comments other than the task-path comments that `schtasks` writes
  is refused, and a hand-edited comment layout can be refused or misread.
- The exit 70 traceback shows the text of the exception. Read it before you paste it in a public
  place.
- A record nested thousands of levels deep ends the run with exit 70 and a traceback instead of a
  refusal.
- Task paths are printed and stored as the scheduler lists them, and some tasks have a user SID in
  the name, so do not paste output or a record into a public place without reading it.
- A record lists task paths and your patterns. Keep it outside any repository, or add it to
  `.gitignore`, before you commit, because the `.gitignore` here excludes no `*.json`.
- `UNVERIFIED` means the task did not read back as disabled. That includes a task that vanished
  during the freeze.
- Exit 1 covers both a task that failed to change and drift or a still-running task. Read the
  printed lines to tell them apart.
- `--action` and `--from-xml` were run against a real saved listing during review. Their output is
  not quoted here.
- A `--from-xml` listing saved as UTF-16 with no byte-order mark is refused with exit 2 and the
  message `the listing holds no task definitions`. Save it again with a byte-order mark or from cmd.
- A failed trim or thaw rewrite can leave a stale `.tmp` file beside the record. The tool never
  reads it, and you can delete it.
- A freeze that hits a full disk while it writes the record exits 2 with `nothing was disabled`
  but leaves a truncated record. Delete that file before the next freeze, which refuses with
  `the manifest file already exists`.
- A `--from-xml` listing goes to Python's standard XML parser and counts as trusted input. A
  hand-edited file with a DOCTYPE or an entity bomb is not rejected up front, so read only
  listings you exported yourself.
- A task whose status reads `Could not start` blocks its folder until you give it to `--except`.
  This is a usability cost, not a wrong verdict. A `Queued` task refuses the freeze only while it
  stays queued, and `--wait` handles it as it handles a running task.
- The Windows host table was checked on Python 3.13.2, 3.12.10 and 3.9.25. Any other patch level was not run.
- Error lines show the scheduler's stderr unchanged. If you point `--schtasks` at a wrapper, make
  sure it writes no secret to stderr.
- The record gets the folder's default permissions. Keep it in a folder that only administrators
  can read.
- The temporary record file is opened without a symbolic-link check. Use a record folder that only trusted
  accounts can write to.
- The tool writes no CSV and only reads the `schtasks` CSV, so spreadsheet formula injection does
  not apply.

## Contributing

Open an issue or pull request on GitHub.

## Author

Built by [Asir Khan](https://www.linkedin.com/in/asir-khan-310317264/).

## License

MIT.

## Related

Other single-file tools in this portfolio for scheduled work:

- [taskpulse](https://github.com/uhsear/taskpulse) - audit every Windows scheduled task and report which ones are silently failing. Read only
- [taskorder](https://github.com/uhsear/taskorder) - tell "the refresh never ran" from "nothing changed", and put tasks in the right order
- [stalled](https://github.com/uhsear/stalled) - count the days since a scheduled notebook or webhook last succeeded
- [jobharness](https://github.com/uhsear/jobharness) - logging, retry and resume for a scheduled script
