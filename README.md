# sshtemp

Template-driven SSH automation using Paramiko `invoke_shell`. Built for **network devices** (prompts, jump hosts, captures, configuration) and works on Linux-style interactive shells.

Requires **Python 3.11+**.

AI assistants writing templates: follow [`AGENTS.md`](AGENTS.md).

```bash
pip install sshtemp
```

## Quick start

```python
from sshtemp import ssht

TEMPLATE = r"""
_HOST: 10.0.0.1
_USERNAME: admin
_PASSWORD: NokiaSros1!
_EXPECT: #\s*$
_CAPTURE:show_version
show version
_END
"""

result = ssht(TEMPLATE)
if result.success:
    print(result.captures["show_version"])
else:
    print("unsuccessful:", result.error)
for warning in result.warnings:
    print(f"warning: {warning}")
```

`ssht()` **never raises**. Check `result.success`, then `result.error` / `result.warnings`, then `result.captures`. Pass `save_to_disk=True` to write capture files and `sshtemp.log` under `result.output_dir`.

CLI (always writes an output folder; `${VAR}` in `.txt` files is filled from the environment, or prompted if missing):

```bash
sshtemp my_template.txt
sshtemp --check my_template.txt
sshtemp -p 4 my_template.txt
```

| | CLI (`sshtemp`) | Python (`ssht()` / `ssht_parallel()`) |
| --- | --- | --- |
| Template | File on disk | Multi-line string, or a single-line file path |
| Captures | Files under `output_*/` | `result.captures` (also on disk when `save_to_disk=True`) |
| Secrets | `${VAR}` from env / prompt | Build the string with `os.environ` (`${VAR}` is **not** expanded) |

Full flags, result fields, and the template language: [Advanced](#advanced).

## Examples

Lab IPs and passwords — adapt them. These files are in the git repository (`examples/`), not in the pip package.

| Level | Python | CLI |
| --- | --- | --- |
| Direct device | `examples/01-basic-nokia_sros.py` | `examples/01-basic-nokia_sros-cli.txt` |
| Jump host | `examples/02-basic-jumpserver-nokia_sros.py` | `examples/02-basic-jumpserver-nokia_sros-cli.txt` |
| Multi-device (sequential) | `examples/03-other-jumpserver-nokia_sros.py` | `examples/03-other-jumpserver-nokia_sros-cli.txt` |
| Multi-device (parallel) | `examples/04-parallel-jumpserver-nokia_sros.py` | `examples/04-parallel-jumpserver-nokia_sros-cli.txt` |

```bash
sshtemp --check examples/01-basic-nokia_sros-cli.txt
export JUMP_PASSWORD=clab
export DEVICE_PASSWORD=NokiaSros1!
sshtemp examples/03-other-jumpserver-nokia_sros-cli.txt
sshtemp -p 2 examples/04-parallel-jumpserver-nokia_sros-cli.txt
```

Default `host_key_policy` is `auto` (unknown SSH host keys are accepted) for the Paramiko `_HOST:` hop only. Nested `ssh` in the template is yours to lock down. Details: [Security](#security).

---

## Advanced

- [CLI](#cli)
- [API](#api)
- [Result](#result)
- [Template language](#template-language)
- [Jump host example](#jump-host-example)
- [Defaults](#defaults)
- [Logging](#logging)
- [Secrets](#secrets)
- [Security](#security)
- [Parallel execution](#parallel-execution)
- [Troubleshooting](#troubleshooting)

### CLI

```bash
sshtemp my_template.txt
sshtemp my_template.txt -v
sshtemp my_template.txt -o output/lab
sshtemp --host-key-policy reject my_template.txt
sshtemp -p 4 my_template.txt
sshtemp --check my_template.txt
```

| Flag | Description |
| --- | --- |
| `--version` | Print the installed version and exit |
| `--check` | Sequential syntax only; no SSH, no `${VAR}` substitution; does not check parallel `_HOST` sections |
| `-v`, `--verbose` | Log INFO events (commands, captures, expect / if-then) |
| `-o DIR`, `--output-dir DIR` | Output folder for capture files and `sshtemp.log` |
| `--host-key-policy auto\|reject` | Paramiko host key policy (default `auto`) |
| `-p N`, `--parallel N` | Run each `_HOST` section concurrently (`N` >= 1) |

`--check` does **not** split on `_HOST:` or inject sticky settings. A file can pass `--check` and still fail `sshtemp -p N`. If both `--check` and `-p` are set, **only `--check` runs**.

Exit `0` prints `output: <path>` on stdout. Warnings and failures go to stderr (partial capture names, failed line when available; for `-p`, each failed section then a summary of the first failure). Without `-o`, the CLI creates `output_YYYYMMDD-HHMMSS/` in the current directory. Repeated capture names in one run are **appended**.

### API

```python
ssht(
    template,
    directive_prefix="_",
    *,
    log_id=None,
    verbose=False,
    save_to_disk=False,
    output_dir=None,
    host_key_policy="auto",
    manage_logging=True,
) -> SshTempResult
```

| Parameter | Default | Description |
| --- | --- | --- |
| `log_id` | `None` | Log line prefix; overridden by `_LOGID:` |
| `verbose` | `False` | Log INFO events when `True` |
| `directive_prefix` | `"_"` | Must not be empty |
| `save_to_disk` | `False` | Write capture files and `sshtemp.log` |
| `output_dir` | `None` | Folder when `save_to_disk=True`; default `output_YYYYMMDD-HHMMSS` |
| `host_key_policy` | `"auto"` | `auto` (accept unknown keys) or `reject` (system `known_hosts` only) |
| `manage_logging` | `True` | `False` skips `configure_logging` / `shutdown_logging` (parallel workers) |

A **multi-line** string is an inline template. A **single-line** string is a file path. Valid templates need at least two lines. `${VAR}` is **not** expanded by `ssht()` — see [Secrets](#secrets).

**`ssht()` never raises.** Fatal problems set `result.success=False` and `result.error`. Exception classes in `sshtemp.exceptions` are internal.

`ssht_parallel()` takes the same `template` / `verbose` / `save_to_disk` / `output_dir` / `host_key_policy` / `directive_prefix` as `ssht()`, plus required `max_workers`. It has no `log_id` or `manage_logging`. See [Parallel execution](#parallel-execution).

#### `validate_template`

```python
from sshtemp import validate_template

result = validate_template(template_text)
if not result.valid:
    for issue in result.issues:
        print(issue)
```

Takes **inline text**, not a file path. Same sequential checks as `sshtemp --check` (placeholders allowed; no parallel prep). Syntax problems are errors (`result.valid` is `False`). Missing `_EXPECT` is a **warning**. `--check` prints warnings to stderr and still exits `0` when syntax is valid. `ssht()` copies validation warnings into `result.warnings` before SSH starts.

### Result

1. `result.success` is `False` → `result.error`.
2. `result.warnings` is not empty → `_ERROR` with `_ERRORFAIL: off`; no `_EXPECT`; or a capture saved because the run failed before `_END`.
3. Otherwise → `result.captures` (and `result.output_dir` when `save_to_disk=True`).

| Field | Description |
| --- | --- |
| `success` | `False` when the run stopped on a fatal error |
| `error` | Fatal message; `None` when `success` is `True` |
| `warnings` | Non-fatal issues (see above) |
| `captures` | Name → output; includes a partial open block if the run failed before `_END` |
| `output_dir` | Set when `save_to_disk=True` |
| `host` | Last `_HOST:` (Paramiko target, not nested `ssh` targets) |
| `log_id` | From `ssht(..., log_id=...)` or `_LOGID:` |
| `failed_line` | Template line at failure; `None` if not applicable |
| `failed_command` | Command at failure |
| `failed_capture_name` | Active `_CAPTURE` name, or `None` |

The same capture name twice in one run **appends**. Warnings and errors are also logged (stderr, plus `sshtemp.log` when `save_to_disk=True`).

### Template language

Lines are processed top to bottom. Directive names are **exact**: `_ENDING` is not `_END`.

| Connect | Session (sticky) | Scoped |
| --- | --- | --- |
| `_HOST:` | `_LOGID:` | `_WAIT:` / `_RETRY:` |
| `_USERNAME:` | `_SHELL:` | `_IF:` / `_THEN:` |
| `_PASSWORD:` | `_SETTLE:` | `_SLEEP:` |
| `_PORT:` | `_RECV_BUFFER:` / `_ENCODING:` | `_ENTER` |
| `_TIMEOUT:` | `_EXPECT:` | `_CAPTURE` / `_END` |
| | `_ERROR:` / `_ERRORFAIL:` | `_# comment` |

Connect settings tell Paramiko how to open the session. They are reused on a later `_HOST:` if omitted. Session settings stay until you change them. Scoped directives apply to the next command, a capture block, or a single line.

#### Connect

**Not** applied to nested `ssh` inside the shell.

| Directive | Example | Description |
| --- | --- | --- |
| `_HOST:` | `_HOST: 10.0.0.1` | Paramiko target. A new value closes the session and reconnects on the next command |
| `_USERNAME:` | `_USERNAME: admin` | SSH username for Paramiko |
| `_PASSWORD:` | `_PASSWORD: secret` | Password; empty → key/agent auth |
| `_PORT:` | `_PORT: 22` | Integer 1–65535 (default `22`) |
| `_TIMEOUT:` | `_TIMEOUT: 10` | Connect timeout in seconds; integer >= 1 (default `10`) |

**Jump host:** `_HOST` is the jump box. Reach devices with a normal `ssh admin@device` command. `_HOST` / `result.host` do not change nested SSH targets.

**Reconnect:** Only `_HOST:` reopens Paramiko. Changing `_USERNAME:`, `_PASSWORD:`, or `_PORT:` stores the value until the next `_HOST:`.

After `invoke_shell` (including reconnect) the engine reads login MOTD **before** the first command. That text is not captured and is not checked against `_ERROR`. Nested `ssh` is a normal command — `_EXPECT` applies to **that** command’s output. Set `_EXPECT` (and `_SETTLE` if the banner is slow) before the first command you care about.

#### Session (sticky)

| Directive | Example | Description |
| --- | --- | --- |
| `_LOGID:` | `_LOGID: myhost2` | Log prefix; overrides `ssht(..., log_id=...)` |
| `_SHELL:` | `_SHELL: 240x72` | PTY size at next `invoke_shell` (default `240x72`) |
| `_SETTLE:` | `_SETTLE: 5` | Seconds to wait after `invoke_shell` for the first login byte, and again for silence after MOTD (integer >= 0, default `2`). `0` drains buffered data only. Set it before the first command (or before a later `_HOST:` reconnect). Does not change `_WAIT` |
| `_RECV_BUFFER:` | `_RECV_BUFFER: 65535` | Bytes per `recv`; integer >= 1 |
| `_ENCODING:` | `_ENCODING: utf-8` | Decode codec (invalid bytes ignored) |
| `_EXPECT:` | `_EXPECT: #\s*$` | Wait until regex matches this command’s output |
| `_ERROR:` | `_ERROR: (?i)minor:` | Match in the **current** command output; non-empty also turns `_ERRORFAIL` on; empty clears the pattern and turns `_ERRORFAIL` off |
| `_ERRORFAIL:` | `_ERRORFAIL: off` | `on` / `off`; put `off` **after** `_ERROR:` for warn-only configure |

Clear `_ERROR:` after a configure block so later `show` / backup lines are not checked.

#### `_WAIT` / `_RETRY`

| Directive | Example | Description |
| --- | --- | --- |
| `_WAIT:` | `_WAIT: 30` | Max wall-clock seconds per command attempt; integer >= 1 (default `20`) |
| `_RETRY:` | `_RETRY: 2` | Max attempts when `_EXPECT` is set; integer >= 1 (default `1`) |

Scope: next command only, then defaults; **or** the whole `_CAPTURE` block when placed immediately before `_CAPTURE` (reset after `_END`).

```text
_WAIT: 120
_CAPTURE:backup
admin display-config
_END
configure
```

`admin display-config` uses 120s; `configure` uses 20s.

**`_RETRY` re-sends the same line.** Use it for `show` / `admin display-config`. Do **not** set it above `1` for configuration, commits, or a password sent as a plain line.

#### One-shot and auto-response

| Directive | Example | Description |
| --- | --- | --- |
| `_SLEEP:` | `_SLEEP: 2` | Block N seconds **at this line** (integer >= 0); before the first command this is before connect |
| `_ENTER` | `_ENTER` | Send an empty line once |
| `_# comment` | `_# note` | Ignored |
| `_IF:` | `_IF: .*assword:` | Regex on the **next** command’s output. Use `.*assword:` (colon) so an `ssh` echo of `PreferredAuthentications=password` does not match |
| `_THEN:` | `_THEN: mypassword` | Sent once when `_IF` matches; pairs are cleared after that command |

#### Capture

```text
_CAPTURE:config_backup
admin display-config
_END
```

`_CAPTURE:name` sets the name (and file name under the output folder). `_CAPTURE` without a name uses `capture_0.txt`, `capture_1.txt`, …. Subfolders are allowed (`version/10.1.1.1.txt`). Absolute paths and `..` are rejected when writing files (not part of `--check`). Close every block before the next `_HOST:`. The first command in a block is sent with a leading newline.

If the run fails before `_END`, output so far is still in `result.captures` (and on disk when `save_to_disk=True`) with a “saved partially” warning.

For a large dump: paging off (`environment no more` on Nokia classic CLI), then `_WAIT: 120` / `_RETRY: 2` immediately before `_CAPTURE`, and keep `_ERROR` off until configure.

Any non-directive line is sent to the shell with a trailing newline.

### Jump host example

```text
_HOST: 172.29.112.112
_USERNAME: clab
_PASSWORD: clab
_PORT: 2222
_EXPECT: #\s*$

_IF: continue connecting
_THEN: yes
_IF: .*assword:
_THEN: NokiaSros1!
ssh -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null admin@10.0.0.3
environment no more

_WAIT: 120
_RETRY: 2
_CAPTURE:running_config
admin display-config
_END

_ERROR:(?i)(critical:|error:|minor:|major:)
configure
    port 1/1/1
        description "lab"
exit all
_ERROR:
```

### Defaults

| Setting | Value |
| --- | --- |
| `_PORT` | `22` |
| `_TIMEOUT` | 10 seconds |
| `_SETTLE` | 2 seconds |
| `_WAIT` | 20 seconds |
| `_RETRY` | 1 attempt |
| `_ERRORFAIL` | `off` until a non-empty `_ERROR:` (then `on`; empty `_ERROR:` clears both) |
| `_EXPECT` / `_ERROR` | empty |
| `_SHELL` | 240 × 72 |
| `_RECV_BUFFER` | 65535 bytes |
| `_ENCODING` | `utf-8` |
| SSH keepalive | 60 seconds |
| `host_key_policy` | `auto` |

### Logging

| API | Log file |
| --- | --- |
| `ssht()` / `ssht_parallel()` with `save_to_disk=False` | None — stderr only |
| `save_to_disk=True` or CLI | `sshtemp.log` in the run output folder |

- `verbose=False`: WARNING and ERROR. `_RETRY` misses log as `sshtemp.command_retry` (**WARNING**), including `output_preview` (can contain a password echo).
- `verbose=True`: also INFO for commands, captures, expect / if-then. A password sent as a **plain template line** can appear in `command=`.
- Log file lines have a UTC timestamp; stderr does not.
- `_LOGID:` / `log_id=` prefixes every line.

Attach your own handler to the `sshtemp` logger (not the root). Records do not propagate. Your handler’s level applies; `verbose` only changes sshtemp’s own handlers.

```python
import logging

handler = logging.FileHandler("myapp.log")
handler.setLevel(logging.INFO)
logging.getLogger("sshtemp").addHandler(handler)
```

### Secrets

Keep passwords out of git.

**CLI** — `${VAR}` in `.txt` files:

```text
_PASSWORD: ${jump_server_password}
```

```bash
export jump_server_password='clab'
sshtemp router.txt
```

Names: letter or `_`, then letters, digits, `_`. Hyphens are a validation error. `${VAR}` on `_#` lines is not substituted. Missing values are prompted (masked). Linux: name match is case-sensitive. Windows: case-insensitive. `--check` does not read the environment.

**Python** — `ssht()` does not expand `${VAR}`:

```python
import os
from sshtemp import ssht

password = os.environ["NOKIA_PASSWORD"]
result = ssht(rf"""
_HOST: 10.0.0.1
_USERNAME: admin
_PASSWORD: {password}
_EXPECT: #\s*$
show version
""")
```

### Security

sshtemp is a thin Paramiko wrapper. **Trust, credentials, and network safety are your responsibility.**

| Topic | Behavior |
| --- | --- |
| **Host keys** | Default `auto`: unknown keys accepted (`AutoAddPolicy`). System `known_hosts` is still checked when present. `reject` refuses unknown hosts (Paramiko `_HOST:` only, not nested `ssh`). |
| **Credentials** | Prefer `${VAR}` or `os.environ`. Empty `_PASSWORD:` uses key/agent. Verbose logs and `_RETRY` `output_preview` can include session text. |
| **Nested `ssh`** | Options such as `StrictHostKeyChecking=no` are **your** template; sshtemp does not rewrite them. |
| **Untrusted path** | Use `reject`, VPN, and verified keys on every hop you control. |

### Parallel execution

`ssht_parallel()` / `sshtemp -p N` run each `_HOST` section concurrently. Sequential `ssht()` is unchanged.

```python
ssht_parallel(
    template,
    max_workers,
    directive_prefix="_",
    *,
    verbose=False,
    save_to_disk=False,
    output_dir=None,
    host_key_policy="auto",
) -> SshTempParallelResult
```

| Parameter | Default | Description |
| --- | --- | --- |
| `template` | (required) | Same as `ssht()`: multi-line string, or a single-line file path |
| `max_workers` | (required) | Concurrent sections; integer `>= 1` |
| `directive_prefix` | `"_"` | Must not be empty |
| `verbose` | `False` | Log INFO events when `True` |
| `save_to_disk` | `False` | Write capture files and `sshtemp.log` |
| `output_dir` | `None` | Shared folder when `save_to_disk=True` |
| `host_key_policy` | `"auto"` | Same as `ssht()` |

No `log_id` — set `_LOGID:` in each section. Logging is configured by the orchestrator (`manage_logging` is not a parameter).

```python
from sshtemp import ssht_parallel

result = ssht_parallel(template, max_workers=4, save_to_disk=True)
if result.success:
    for section_result in result.results:
        print(section_result.host, section_result.captures)
else:
    print(result.error)
```

**`ssht_parallel()` never raises.** `result.results` is one `SshTempResult` per section, in template order. There is no merged capture dict. All sections are started; one failure does not cancel the others.

| Field | Description |
| --- | --- |
| `success` | `True` only when every section succeeded |
| `results` | `list[SshTempResult]` in `_HOST` order |
| `output_dir` | Shared folder when `save_to_disk=True` |
| `error` | Prep failure, or summary of the first failed section |
| `failed_section_index` | 0-based index of the first failure |

Workers share one log file when `save_to_disk=True`. Use unique capture names per device and `_LOGID:` per section. The orchestrator configures logging once; workers use `manage_logging=False`.

#### Template preparation

1. Split on each `_HOST:` line.
2. For later sections, insert missing stickies **right after** that section’s `_HOST:` from earlier sections.

**Injected when missing:** `_USERNAME`, `_PASSWORD`, `_PORT`, `_TIMEOUT`, `_SHELL`, `_SETTLE`, `_RECV_BUFFER`, `_ENCODING`, `_EXPECT`.

**Not injected:** `_LOGID`, `_ERROR` / `_ERRORFAIL`, `_WAIT` / `_RETRY`, `_IF` / `_THEN`. Wrap configure `_ERROR:` … `_ERROR:` in **each** section.

Each section must have balanced `_CAPTURE` / `_END`. Content before the first `_HOST:` is a parallel-only error (`--check` does not catch it).

### Troubleshooting

| Symptom | Likely cause | What to try |
| --- | --- | --- |
| `expect pattern ... not matched within ...s` | Prompt too slow or regex wrong | Increase `_WAIT:`; `_RETRY: 2` on read-only commands; Nokia classic CLI: `_EXPECT: #\s*$` (a line that is only `#` also matches — tighten if a dump contains that) |
| `channel closed before expect pattern ... matched` | Session ended (`logout` / drop) | Drop `_EXPECT` on `logout`, or find why the session died |
| Commands feel slow with no `_EXPECT` | Each line waits full `_WAIT` (20s) | Set `_EXPECT:` to the device prompt |
| `failed to connect to host:port` | IP / port / SSH down | Check `_HOST:` `_PORT:`; test with `ssh` |
| Auth hang | Nested login | `_PASSWORD:`; `_IF:` / `_THEN:` immediately before `ssh` |
| Nested password never sent | `_IF: .*assword` matched the `ssh` echo | Use `_IF: .*assword:` (colon) |
| `template validation failed` | Syntax | `sshtemp --check` or `validate_template(text)` |
| `--check` ok but `-p N` fails | Parallel-only prep | See [Template preparation](#template-preparation) |
| `failed to read template` | Not UTF-8 or unreadable | Save as UTF-8 |
| `failed to write output` | Disk / permissions | Check `-o` / `output_dir` |
| `invalid capture path` | Absolute or `..` | Relative name, e.g. `version/10.1.1.1.txt` |
| `_HOST inside an open _CAPTURE block` | Missing `_END` | Close the capture before `_HOST:` |
| Missing `${VAR}` | Env name mismatch | `export var_name=value` (Windows: case-insensitive) |
| `invalid placeholder ${my-var}` | Hyphen in name | Use `${my_var}` |
| Run succeeds but the box printed errors | `_ERRORFAIL: off` | See `result.warnings` |
| Jump works, device commands fail | `_HOST` is the jump | `ssh admin@device` after connect |
| First capture starts with MOTD | Banner after `_SETTLE`, or `_CAPTURE` around `ssh` | Increase `_SETTLE:`; capture after login/`ssh` |
| Capture empty or partial | Timeout / close before `_END` | Increase `_WAIT` / `_RETRY`; check `_EXPECT`. Partial output is still in `result.captures` |

## Tests

```bash
python -m unittest discover -s tests -p "test_*.py"
```
