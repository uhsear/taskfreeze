# taskfreeze

Disable a set of Windows scheduled tasks for a maintenance window, and later turn back on exactly
the ones it turned off. It refuses to write an empty record, and it never enables a task that was
already off before the freeze.

The script everyone writes for a maintenance window looks like this:

```powershell
$tasks = Get-ScheduledTask -TaskPath '\Nightly\'
$tasks | Where-Object State -ne 'Disabled' | Export-Clixml freeze.xml     # "the tasks I changed"
$tasks | Disable-ScheduledTask
# ... patch the server ...
Import-Clixml freeze.xml | Enable-ScheduledTask
```

Two things go wrong, and both show up weeks later.

- **The second freeze.** The window slips a day, someone runs the freeze again, and every task is
  already off. The script finds no enabled task and writes a record of zero tasks over the first
  record. The thaw prints "0 of 0 restored", exits 0, and the nightly jobs stay off. Nobody
  notices until the report that depends on them goes stale.
- **The blanket enable.** The thaw enables "everything in scope". One task in that folder was
  switched off on purpose a year ago, because the feed it reads was retired. The window turns it
  back on, and it runs against a source that no longer exists.

This tool refuses both. A freeze that has nothing to disable exits 3 and writes no file. A freeze
never overwrites a record that exists. A task stays in the record only if the tool saw it
enabled, disabled it, and read it back as disabled. The thaw touches only the tasks in the record.
A record that was already thawed cannot be thawed again, because the second thaw could enable a
task that someone disabled in the meantime.

```
python taskfreeze.py --name "^[\\]Nightly[\\]" --manifest freeze1.json            # plan only
python taskfreeze.py --name "^[\\]Nightly[\\]" --manifest freeze1.json --apply    # freeze
python taskfreeze.py --status freeze1.json                                        # read only
python taskfreeze.py --thaw freeze1.json --apply                                  # restore
```

Nothing changes without `--apply`. Python 3.9 or later, standard library only. The scheduler part
runs on Windows. The self-test also runs on Linux, and `--from-xml` plans from a saved listing
there.

## What a real run looks like

This is a real session on Windows 11 (build 26100), against three scratch tasks the run created
and deleted afterwards. Two were enabled (`a`, `b`). One (`pre-off`) was disabled before the freeze.
The pattern is `^[\\]taskfreeze-dev[\\]`.

```
$ taskfreeze --name "^[\\]taskfreeze-dev[\\]" --manifest m.json
leave    \taskfreeze-dev\pre-off  (already disabled, stays as it is)
disable  \taskfreeze-dev\a
disable  \taskfreeze-dev\b
2 to disable, 1 already off and left alone. Manifest: m.json
nothing changed. Add --apply to disable them.
rc=0

$ taskfreeze ... --manifest m.json --apply
disabled \taskfreeze-dev\a
disabled \taskfreeze-dev\b
froze 2 of 2 task(s). Manifest: m.json
rc=0

$ taskfreeze ... --manifest m2.json --apply        (the second freeze, a new path)
matched 0 enabled and 3 already disabled task(s); nothing was changed.
refused: every matched task is already disabled, so the manifest would be empty and a thaw would
restore nothing. A freeze may still be in force: check --status on its manifest.
rc=3

$ taskfreeze ... --manifest m.json --apply         (the second freeze, the same path)
refused: the manifest file already exists. A second freeze must not overwrite the record of the
first. Thaw it, or choose a new path.
rc=3

$ taskfreeze ... --manifest m3.json --ap           (a prefix of --apply)
taskfreeze: error: unrecognized arguments: --ap
rc=64

$ taskfreeze --thaw m.json --apply
enable   \taskfreeze-dev\a
enable   \taskfreeze-dev\b
left     \taskfreeze-dev\pre-off  (off before the freeze, stays off)
2 of 2 task(s) in the manifest are enabled: 2 enabled now, 0 already on, 0 missing
rc=0

$ taskfreeze --thaw m.json --apply                 (again)
refused: this manifest was already thawed at 2026-10-23T04:46:42Z. A second thaw could re-enable a
task someone has disabled since.
rc=3
```

After the thaw, `a` and `b` read `Ready` and `pre-off` still reads `Disabled`. The refused second
freeze left no `m2.json` behind. The `--status` call between the freeze and the thaw printed
`OFF` for `a` and `b` and exited 0. The lines are cut only where the text says so.

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
- **Other GitHub projects.** `gh search repos` on 2026-10-23 for `taskfreeze`, `freeze scheduled
  tasks`, `disable scheduled tasks restore manifest`, `task scheduler freeze thaw`,
  `schtasks disable enable` and `scheduled task maintenance window pause resume` returned empty
  lists. A search for `disable-scheduledtask` found
  `SkylineCommunications/SLC-AS-Enable-Disable-ScheduledTasks`, a C# repository with no
  description and a readme that holds only its title, so this README cannot say what it does. That shows only that the search found
  nothing. It does not show that nothing exists.

## Requirements

- Python 3.9 or later. Standard library only. No credentials, no network.
- For `--apply` on Windows: `schtasks.exe` on the path and the rights `/change` needs (see above).
- `--from-xml` reads a saved listing, so a plan can run on any machine, including Linux.

## How a task is read

The tool runs `schtasks /query /xml`. The command "outputs all task definitions on the system to XML
format"
([schtasks query](https://learn.microsoft.com/en-us/windows-server/administration/windows-commands/schtasks-query)).
Three details decide whether a task counts as enabled.

- **Only `Settings/Enabled` counts.** The element is a boolean with `default="true"`
  ([Enabled, settingsType](https://learn.microsoft.com/en-us/windows/win32/taskschd/taskschedulerschema-enabled-settingstype-element)).
  A trigger has its own `Enabled` element. Read that one by mistake and a disabled task looks
  live. The self-test pins this.
- **A missing element means enabled.** That is the schema default. On the Windows 11 machine used
  here, `schtasks /query /xml` left the element out of every task that was enabled.
- **The value must be a legal `xs:boolean`.** The lexical space is `true`, `false`, `1` and `0`, and
  white space is collapsed ([XML Schema 1.1 Part 2, section 3.3.2](https://www.w3.org/TR/xmlschema11-2/#boolean)).
  Anything else, including `FALSE`, stops the run with an error. A guess could read a disabled task
  as enabled.

The listing is refused, not partly read, when it holds no task, repeats a task path, or holds a
definition with no path comment above it. A task the selectors cannot see would never be frozen.

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
| `--schtasks PROGRAM` | The scheduler program. Default `schtasks`. |
| `--apply` | Change the scheduler and write the record. Without it, nothing is written. |
| `--self-test` | Run the offline assertions and exit. |

The parser has `allow_abbrev=False`. A prefix such as `--ap` is refused with exit 64 instead of
running as `--apply`. The self-test pins `--ap`, `--app` and `--appl`, and a prefix of every other
long flag.

Exit codes:

| Code | Meaning |
|---|---|
| 0 | Done, or a plan with nothing wrong. |
| 1 | Not everything was done or verified: a task failed to change, did not read back as changed, or is missing. `--status` also exits 1 on drift. |
| 2 | The scheduler or a file could not be read or written. |
| 3 | Refused: nothing to freeze, a record already exists, or the record is empty or spent. |
| 64 | Usage error. |
| 70 | An unexpected crash in the tool. |

### What the record holds

A JSON file with `disabled` (tasks this freeze turned off), `already_off` (matched tasks that were
off before, left alone), the three patterns, a UTC timestamp, a `count` and a `thawed` time that is
`null` until a thaw finishes. The thaw checks the record before it touches anything. It refuses a
record with an empty `disabled` list, a `count` that disagrees with the list, a task in both lists,
a repeated task, a task path with no leading backslash, or an unknown `version`.

### Order of events in a freeze

1. Read the scheduler and plan. Refuse if the record exists, nothing matched, a matched name lost a
   character to the console code page, or no matched task is enabled.
2. Create the record with mode `x`, which fails if the file exists. It is written before the first
   change, so a crash half way never leaves a disabled task with no record.
3. Disable each task, then read the scheduler back.
4. Keep in the record only the tasks that read back as disabled. A `/change` that reported an
   error but took effect is kept, so the thaw restores it. A `/change` that reported success but
   changed nothing is dropped and reported as `UNVERIFIED`. If nothing was disabled, the record is
   removed and the exit code is 1.

The thaw reads back the same way. It marks the record thawed only when every listed task is
enabled, so a re-run after a partial thaw retries the rest.

## Self-test

`python taskfreeze.py --self-test` runs offline. It builds listings in the shape `schtasks` prints,
and it drives the real subprocess path through a stand-in `schtasks` program that keeps its tasks
in a JSON file and can be told to fail, to ignore a change, to change and then error, or to hang.

```
taskfreeze self-test: temporary files and a stand-in schtasks, no network
--------------------------------------------------------------------
PASS  a disabled task whose triggers all say Enabled true is still disabled  <-- pinned defect
PASS  an empty listing is refused, not read as zero tasks  <-- pinned defect
...
PASS  a freeze whose matches are all already off is REFUSED, because its manifest would be empty  <-- pinned defect
PASS  a freeze onto a manifest that exists is REFUSED  <-- pinned defect
...
PASS  the task that was off BEFORE the freeze is still off after the thaw  <-- pinned defect
PASS  a second thaw is REFUSED and does not re-enable a task disabled since  <-- pinned defect
...
PASS  a /change that exits 0 and changes nothing is UNVERIFIED and is not recorded  <-- pinned defect
PASS  a /change that errors after taking effect is recorded, so the thaw restores it  <-- pinned defect
...
PASS  --thaw of an empty manifest exits 3, never '0 of 0 restored'  <-- pinned defect
...
PASS  the prefix --ap of --apply is REFUSED by the parser  <-- pinned defect
...
PASS  check() and raises() record failures, and the footer names them and exits 1  <-- pinned defect
PASS  an all-green harness exits 0 with the 0 failed footer
--------------------------------------------------------------------
179 assertions, 0 failed
```

The full run prints all 179 assertions. Each `...` line is where this block is cut.

| Host | Python | Assertions |
|---|---|---|
| Windows 11 | 3.13 | 179, 0 failed |
| Windows 11 | 3.12 | 179, 0 failed |
| Windows 11 | 3.9.25 | 179, 0 failed |
| Ubuntu | 3.12.3 | 179, 0 failed |

The counts are equal because no assertion depends on the host. The default codec assertion checks
`oem` on Windows and `utf-8` elsewhere in one assertion that passes on both.

Branch coverage, measured on Windows with `coverage run --branch taskfreeze.py --self-test`: 893
statements, 202 branches, 0 missed, 100 percent. `coverage` is not installed on the Linux host, so
Linux coverage was not measured. `pyflakes` is clean and `python -W error` compiles the file.

### Do the assertions fail when the logic is wrong

A passing self-test says little if it cannot go red. Each of these edits to a copy of the script made
the self-test fail:

| Edit | Failing assertions |
|---|---|
| Drop the refusal when no matched task is enabled | 2 |
| Drop the "already thawed" refusal | 3 |
| Let the thaw also walk `already_off` | 14 |
| Set `allow_abbrev=True` | 6 |
| Drop the empty-`disabled` check in manifest validation | 4 |

## Why the obvious version is wrong

The obvious freeze marks "tasks I changed" as "tasks that matched". The first run is fine. The
second run matches the same tasks, finds them off, and either records them again (the thaw then
enables tasks that the first freeze owns) or records nothing (the thaw restores nothing). This tool
records a task only if it changed it, and it says why when that set is empty.

The obvious thaw reads the exit status of `schtasks /change`. An exit of 0 does not prove the task
changed, and an exit of 1 does not prove it did not. The tool reads the scheduler back and trusts
that. The self-test has a stand-in that does each of the four things, and one assertion per case.

## What it will not do

- It does not stop a task that is running. This was checked on Windows 11: a task started with
  `schtasks /run` read `Running`, `schtasks /change /disable` succeeded, and the task still read
  `Running`. End a running instance yourself with `schtasks /end`.
- It does not work on a remote computer. `schtasks` has `/s`; this tool does not pass it.
- It does not touch the tasks it did not select, and it does not delete a task.
- It does not follow a task that someone renamed between the freeze and the thaw. The thaw reports
  it `MISSING` and exits 1.
- It cannot see a task a group policy or another administrator re-enables during the window. Run
  `--status` before the thaw to see that.

## Limits

- Every line and branch of the script ran in the self-test, so no line is excluded from the
  coverage figure. The subprocess calls in the self-test go to a stand-in program, not to the real
  `schtasks`. The real `schtasks` was exercised by the session above, which is not part of the
  self-test because it creates scheduled tasks.
- The codec for `schtasks` output is the OEM code page on Windows. A name the console cannot carry
  comes back with `?` or U+FFFD, and the tool refuses to freeze such a name, because a `/change` on
  it would address a different task or none.
- The record is a plain file. Anyone who can write it can change what the thaw does. The tool
  validates the record but does not sign it.
- The self-test covers the network-free design only. The tool has no network path.

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
