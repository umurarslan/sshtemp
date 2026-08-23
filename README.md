# sshtemp

Template-driven SSH automation using Paramiko `invoke_shell`. Especially useful for **network devices** (prompts, jump hosts, captures, configuration) and also works on Linux and similar interactive shells.


## Install

```bash
pip install sshtemp
```

## Quick start

Read and capture:

```python
from sshtemp import ssht

TEMPLATE = """
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
```

Configure a port (`_ERROR` stops the run on a match; use `_ERRORFAIL: off` to warn only):

```python
from sshtemp import ssht

result = ssht("""
_HOST: 10.0.0.1
_USERNAME: admin
_PASSWORD: NokiaSros1!
_EXPECT: #\s*$
_ERROR:(?i)(critical:|error:|minor:|major:)
configure
    port 1/1/1
        description "lab test"
exit all
_ERROR:
""")
if not result.success:
    print("unsuccessful:", result.error)
```

Warn-only configure (match adds `result.warnings` but the run continues):

```python
result = ssht("""
_HOST: 10.0.0.1
_USERNAME: admin
_PASSWORD: NokiaSros1!
_EXPECT: #\s*$
_ERROR:(?i)(critical:|error:|minor:|major:)
_ERRORFAIL: off
configure
    port 1/1/1
        description "lab test"
exit all
_ERROR:
""")
if result.success:
    for warning in result.warnings:
        print(f"warning: {warning}")
```

**`_ERROR` rules:** a non-empty `_ERROR:` pattern turns `_ERRORFAIL` on automatically. Put `_ERRORFAIL: off` **after** the `_ERROR:` line to override. Clear both with an empty `_ERROR:` after the configure block.

`ssht()` returns a result object. Check `result.success`, then `result.warnings`, then read `result.captures` (see [Result](#result)). Use `result.output_dir` only when you passed `save_to_disk=True` — that folder holds capture files and `sshtemp.log`.

Optional parameters:

```python
result = ssht(TEMPLATE, log_id="myhost2", verbose=True, directive_prefix="_")
result = ssht("my_template.txt")
result = ssht("my_template.txt", save_to_disk=True, output_dir="output/lab")
result = ssht(TEMPLATE, host_key_policy="reject")
```


| Parameter          | Default  | Description                                                                                    |
| ------------------ | -------- | ---------------------------------------------------------------------------------------------- |
| `log_id`           | `None`   | Log line prefix; overridden by `_LOGID:` in the template                                       |
| `verbose`          | `False`  | When `True`, log INFO events (commands, captures, expect details)                              |
| `directive_prefix` | `"_"`    | Prefix for template directives; must not be empty                                              |
| `save_to_disk`     | `False`  | When `True`, write capture files and `sshtemp.log` to an output folder                         |
| `output_dir`       | `None`   | Output folder when `save_to_disk=True`; default `output_YYYYMMDD-HHMMSS`                       |
| `host_key_policy`  | `"auto"` | Paramiko host key policy: `auto` (accept unknown keys) or `reject` (only system `known_hosts`) |




### CLI vs Python


|                  | **CLI** (`sshtemp`)                      | **Python** (`ssht()`)                               |
| ---------------- | ---------------------------------------- | --------------------------------------------------- |
| Template         | `.txt` file on disk                      | Multi-line string, or single-line file path       |
| `save_to_disk`   | Always `True`                            | Default `False`                                     |
| Captures         | Files under `output_*/`                  | `result.captures`; same data written to disk when `save_to_disk=True` |
| Log file         | `output_*/sshtemp.log`                   | `sshtemp.log` in cwd (unless `save_to_disk=True`)   |
| `${VAR}` secrets | Substituted from env; prompts if missing | **Not supported** — use `os.environ` in your string |
| `--check`        | Syntax only; `${VAR}` left as-is         | `validate_template(text)`                           |


### Examples (Nokia SR OS)

Lab-specific IPs and passwords — adapt templates to your environment.


| Level        | Python                                         | CLI                                              |
| ------------ | ---------------------------------------------- | ------------------------------------------------ |
| Direct device | `examples/01-basic-nokia_sros.py`              | `examples/01-basic-nokia_sros-cli.txt`           |
| Jump host    | `examples/02-basic-jumpserver-nokia_sros.py`   | `examples/02-basic-jumpserver-nokia_sros-cli.txt` |
| Multi-device (sequential) | `examples/03-other-jumpserver-nokia_sros.py`   | `examples/03-other-jumpserver-nokia_sros-cli.txt` |
| Multi-device (parallel) | `examples/04-parallel-jumpserver-nokia_sros.py` | `examples/04-parallel-jumpserver-nokia_sros-cli.txt` |


```bash
sshtemp --check examples/01-basic-nokia_sros-cli.txt
sshtemp examples/02-basic-jumpserver-nokia_sros-cli.txt

export JUMP_PASSWORD=clab
export DEVICE_PASSWORD=NokiaSros1!
sshtemp examples/03-other-jumpserver-nokia_sros-cli.txt
python examples/03-other-jumpserver-nokia_sros.py

sshtemp -p 2 examples/04-parallel-jumpserver-nokia_sros-cli.txt
python examples/04-parallel-jumpserver-nokia_sros.py
```

Level 03 runs devices **sequentially** in one `ssht()` call. Level 04 runs each `_HOST` section **in parallel** with `ssht_parallel()` / `sshtemp -p N`. See also [Example: jump host](#example-jump-host-read-and-configure) for `_ERROR` during configure.

## CLI

Validate a template without connecting (`${VAR}` placeholders are **not** checked or substituted):

```bash
sshtemp --check my_template.txt
```

Run a template file from the terminal (`${VAR}` is read from the environment; missing values are prompted):

```bash
sshtemp my_template.txt
sshtemp my_template.txt -v
sshtemp my_template.txt -o output/lab
sshtemp --host-key-policy reject my_template.txt
sshtemp -p 4 my_template.txt
sshtemp --parallel 4 my_template.txt
```


| Flag                            | Description                                                     |
| ------------------------------- | --------------------------------------------------------------- |
| `--check`                       | Validate template syntax only; no SSH, no `${VAR}` substitution |
| `-v`, `--verbose`               | Log INFO events (commands, captures, expect / if-then details)  |
| `-o DIR`, `--output-dir DIR`    | Output folder for capture files and `sshtemp.log`               |
| `--host-key-policy auto|reject` | Paramiko host key policy (default `auto`)                       |
| `-p N`, `--parallel N`          | Run each `_HOST` section concurrently with at most `N` workers  |


On success, the CLI exits with code `0` and prints `output: <path>` to stdout (the run output folder). Warnings still go to stderr. On failure, stderr includes warnings (if any), partial capture names, output path, failure location when available (`failed at line …`), and the error message.

This creates `output_YYYYMMDD-HHMMSS/` in the current directory when `-o` is not set (same as `ssht("my_template.txt", save_to_disk=True)`). Capture files and `sshtemp.log` for that run are written inside that folder. Each `_CAPTURE` block becomes a file named after the capture (for example `device_10.1.1.1.txt` or `version/10.1.1.1.txt`). Repeated capture names in the same run are **appended** to the same file. Quote template paths that contain spaces, for example `sshtemp "my template.txt"`.

## API

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

Pass a **multi-line** string as an inline template, or a **single-line** string as a path to a template file (for example `"router.txt"`). Valid templates need at least two lines (`_HOST:`, `_USERNAME:`, commands, …). `${VAR}` placeholders are **not** expanded by `ssht()` — build secrets with `os.environ` in Python (see [Secrets](#secrets)). Use the `sshtemp` CLI for `.txt` templates with `${VAR}`.

`manage_logging=False` skips `configure_logging` / `shutdown_logging` for that call (for example parallel workers that configure logging once). Default is `True`.

With `save_to_disk=False` (default), output is in `result.captures` only; the log file is `sshtemp.log` in the current working directory. With `save_to_disk=True`, the same captures are in `result.captures` and on disk under `output_dir` (or a new `output_YYYYMMDD-HHMMSS/` folder), with `sshtemp.log` in that folder.

**`ssht()` never raises.** Fatal problems set `result.success=False` and `result.error`; check those instead of `try/except`. Exception classes (`SshTempTimeoutError`, etc.) exist in `sshtemp.exceptions` for internal engine use — they are not part of the public `ssht()` contract.

### `validate_template`

Check template syntax without opening an SSH connection:

```python
from sshtemp import validate_template

result = validate_template(template_text)
if not result.valid:
    for issue in result.issues:
        print(issue)
for warning in result.warnings:
    print(f"warning: {warning}")
```

CLI equivalent: `sshtemp --check router.txt` (validates the file as-is; `${VAR}` placeholders are allowed). `validate_template()` takes inline text only — it does not load a file path; use `ssht("router.txt")` or read the file yourself first.

**Warnings vs errors:** `validate_template()` and `ssht()` share the same validator. Syntax problems are **errors** (`result.valid` / `result.success` is `False`, `result.issues` or `result.error`). Non-fatal checks (for example no `_EXPECT` before commands) are **warnings** (`result.warnings`). With `--check`, warnings go to stderr and exit code stays `0` when syntax is valid. With `ssht()`, validation warnings are copied into `result.warnings` before the SSH run starts (and any runtime warnings are added later).

For CLI runs, `${VAR}` substitution is handled by the `sshtemp` command before calling `ssht()`.

## Result

After `ssht()` returns, use this order:

1. `result.success` **is** `False` → read `result.error` (fatal: validation, connect, timeout, `_ERROR` match with `_ERRORFAIL` on, …).
2. `result.warnings` **is not empty** → run finished, but check messages: `_ERROR` matched while `_ERRORFAIL` was `off`, or template had no `_EXPECT` (each command waits the full default `_WAIT`).
3. **Otherwise** → read `result.captures`. With `save_to_disk=True`, capture files and `sshtemp.log` are also under `result.output_dir`.

```python
from sshtemp import ssht

result = ssht(TEMPLATE)
if result.success:
    print(result.captures["show_version"])
else:
    print("unsuccessful:", result.error)
for warning in result.warnings:
    print(f"warning: {warning}")
```


| Field                 | Description                                                                                                                                 |
| --------------------- | ------------------------------------------------------------------------------------------------------------------------------------------- |
| `success`             | `False` when the run stopped on a fatal error                                                                                               |
| `error`               | Fatal message; `None` when `success` is `True`                                                                                              |
| `warnings`            | Non-fatal issues: `_ERROR` matched while `_ERRORFAIL` was `off`; or template had no `_EXPECT` (each command waits the full default `_WAIT`) |
| `captures`            | Capture name → output (`dict[str, str]`); always filled on success                                                                         |
| `output_dir`          | Set when `save_to_disk=True` — folder with capture files and `sshtemp.log`                                                                  |
| `host`                | Last `_HOST:` (Paramiko target, not nested `ssh` targets)                                                                                   |
| `log_id`              | From `ssht(..., log_id=...)` or `_LOGID:` in the template                                                                                   |
| `failed_line`         | Template line number at failure; `None` when `success` is `True`                                                                            |
| `failed_command`      | Command at failure; `None` when not applicable                                                                                              |
| `failed_capture_name` | Active `_CAPTURE` name at failure; `None` when not inside a capture block                                                                   |


**Captures:** the same capture name twice in one run **appends** (in `result.captures` and on disk when `save_to_disk=True`).

Warnings and errors are written to `sshtemp.log` when they occur. `result.warnings` is the same non-fatal summary for Python callers and CLI stderr (not a deferred log queue). The CLI prints warnings to stderr on success.

## Troubleshooting


| Symptom                                      | Likely cause                                      | What to try                                                                                     |
| -------------------------------------------- | ------------------------------------------------- | ----------------------------------------------------------------------------------------------- |
| `expect pattern ... not matched within ...s` | Prompt did not appear in time                     | Increase `_WAIT:`; add `_RETRY: 2`; fix `_EXPECT:` regex (for Nokia SR OS try `_EXPECT: #\s*$`) |
| Commands feel slow with no `_EXPECT`         | Each command waits the full `_WAIT` (default 20s) | Set `_EXPECT:` to your device prompt so the next command runs as soon as the prompt returns     |
| `failed to connect to host:port`             | Wrong IP, port, firewall, or SSH down             | Check `_HOST:` and `_PORT:`; test with `ssh user@host -p port` manually                         |
| Auth / password prompts hang or fail         | Credentials or nested SSH login                   | Set `_PASSWORD:`; add `_IF:` / `_THEN:` before the `ssh admin@device` line                      |
| `template validation failed`                 | Syntax error before SSH starts                    | Run `sshtemp --check my_template.txt` or `validate_template(text)` in Python                    |
| Missing `${VAR}` on CLI run                  | Environment variable not set or name mismatch     | `export var_name=value` (Linux: same spelling/case as template; Windows: case-insensitive) or enter when prompted |
| `${my-var}` not substituted                  | Hyphens are not valid in placeholder names      | Use `${my_var}` and `export my_var=value`                                                     |
| Run succeeds but device shows errors         | `_ERROR` matched with `_ERRORFAIL: off`           | Check `result.warnings` or CLI stderr; remove `_ERRORFAIL: off` to stop on match                |
| Jump host works but device commands fail     | `_HOST` is the jump box, not the device           | Use a normal `ssh admin@device` command after connecting to the jump host                       |
| Capture file empty or partial                | Command timed out or prompt never matched         | Increase `_WAIT` / `_RETRY` inside the `_CAPTURE` block; verify `_EXPECT`                       |




## DSL reference

Lines are processed top to bottom. Template directives use `directive_prefix` (default `_`).

### Connect, session, and scoped directives

**Connect** settings tell Paramiko how to open the SSH session. They are not “sticky” in the template sense — you write them to connect. If you omit them on a later `_HOST:` reconnect, the engine reuses the previous values.

**Session** settings stay in memory until you change them (sticky).

**Scoped** directives apply only to the next command, a capture block, or a single line.


| Connect      | Session (sticky)               | Scoped               |
| ------------ | ------------------------------ | -------------------- |
| `_HOST:`     | `_LOGID:`                      | `_WAIT:` / `_RETRY:` |
| `_USERNAME:` | `_SHELL:`                      | `_IF:` / `_THEN:`    |
| `_PASSWORD:` | `_RECV_BUFFER:` / `_ENCODING:` | `_SLEEP:`            |
| `_PORT:`     | `_EXPECT:`                     | `_ENTER`             |
| `_TIMEOUT:`  | `_ERROR:` / `_ERRORFAIL:`      | `_CAPTURE` / `_END`  |
|              |                                | `_# comment`         |




### Connect

Used when Paramiko opens or reopens a session. **Not** applied to nested `ssh` commands inside the shell.


| Directive    | Example             | Description                                                                        |
| ------------ | ------------------- | ---------------------------------------------------------------------------------- |
| `_HOST:`     | `_HOST: 10.0.0.1`   | Paramiko target. A new value closes the session and reconnects on the next command |
| `_USERNAME:` | `_USERNAME: admin`  | SSH username for Paramiko                                                          |
| `_PASSWORD:` | `_PASSWORD: secret` | Password; empty → key/agent auth                                                   |
| `_PORT:`     | `_PORT: 22`         | SSH port (default `22`)                                                            |
| `_TIMEOUT:`  | `_TIMEOUT: 10`      | Connect timeout in seconds (default `10`)                                          |


On a later `_HOST:` reconnect, any connect field you omit is reused from earlier in the template. Example: set `_USERNAME`, `_PASSWORD`, and `_PORT` once for a jump host; on the second device block, only `_HOST` and `_LOGID` need to change.

**Jump host note:** `_HOST` is the jump box. Reach devices with normal shell commands (for example `ssh admin@device`). `_HOST` affects logging and `result.host`, not nested SSH targets.

**Reconnect note:** Only `_HOST:` closes and reopens the Paramiko session. Changing `_USERNAME:`, `_PASSWORD:`, or `_PORT:` updates the stored value but does not reconnect until the next `_HOST:`.

Each new Paramiko session sends SSH keepalive packets every 60 seconds (not configurable).

`_EXPECT` **and connect:** Paramiko connect does not wait for `_EXPECT`. The pattern applies when the engine sends shell commands. After a nested `ssh` to a Nokia device, the prompt usually ends with `#` — `_EXPECT: #\s*$` works for device commands (`show`, `configure`, …). Set it before the first command you care about (typically before `ssh admin@device`).

### Session (sticky until changed)


| Directive       | Example               | Description                                                                      |
| --------------- | --------------------- | -------------------------------------------------------------------------------- |
| `_LOGID:`       | `_LOGID: myhost2`     | Log line prefix; overrides `ssht(..., log_id=...)`                               |
| `_SHELL:`       | `_SHELL: 240x72`      | PTY size at next `invoke_shell` (default `240x72`)                               |
| `_RECV_BUFFER:` | `_RECV_BUFFER: 65535` | Bytes per `recv` call (default `65535`)                                          |
| `_ENCODING:`    | `_ENCODING: ascii`    | Decode shell output with this codec (default `utf-8`; invalid bytes are ignored) |
| `_EXPECT:`      | `_EXPECT: #\s*$`      | Wait until regex matches output (Nokia SR OS: `#` prompt)                        |
| `_ERROR:`       | `_ERROR: (?i)minor:`  | Detect error text in the current command output; non-empty pattern also turns `_ERRORFAIL` on; empty clears the pattern and turns `_ERRORFAIL` off |
| `_ERRORFAIL:`   | `_ERRORFAIL: off`     | Override fail-on-match (`on` / `off`); use `off` immediately **after** `_ERROR:` for warn-only configure blocks                                    |


`_ERROR` is checked only on output from the **current** command. After a configure block, clear with `_ERROR:` so later commands are not checked. Put `_ERRORFAIL: off` **after** the `_ERROR:` pattern line (not before) when you want warnings only.

### Scoped — command timing (`_WAIT` / `_RETRY`)


| Directive | Example     | Description                                      |
| --------- | ----------- | ------------------------------------------------ |
| `_WAIT:`  | `_WAIT: 30` | Max seconds per command attempt (default `20`)   |
| `_RETRY:` | `_RETRY: 2` | Max attempts when `_EXPECT` is set (default `1`) |


Scope:

1. **Next command only** — then reset to defaults (`20` / `1`).
2. **Entire capture block** — when placed immediately before `_CAPTURE`; reset after `_END`.

```text
_WAIT: 120
_CAPTURE:backup
admin display-config
_END
configure
```

`admin display-config` uses `_WAIT: 120`; `configure` uses default `20` with no reset line.

When `_EXPECT` is set and the pattern is not matched within `_WAIT`, the command is re-sent up to `_RETRY` times.

### Scoped — one-shot


| Directive    | Example            | Description                                                                 |
| ------------ | ------------------ | --------------------------------------------------------------------------- |
| `_SLEEP:`    | `_SLEEP: 2`        | Block for N seconds at this line; does not change `_WAIT` or other settings |
| `_ENTER`     | `_ENTER`           | Send an empty line once                                                     |
| `_# comment` | `_# optional note` | Ignored                                                                     |




### Scoped — auto-response (next command only)


| Directive | Example             | Description                              |
| --------- | ------------------- | ---------------------------------------- |
| `_IF:`    | `_IF: .*assword:`    | Regex to watch in output                 |
| `_THEN:`  | `_THEN: mypassword` | Response to send once when `_IF` matches |


Place `_IF` / `_THEN` pairs immediately **before** the command they apply to. Rules are cleared after that command is sent.

### Scoped — capture block

```text
_CAPTURE:config_backup
show running-config
_END
```

`_CAPTURE` starts collecting shell output; `_CAPTURE:name` sets the capture name (and file name under the output folder). Names may include subfolders, for example `_CAPTURE:version/10.1.1.1.txt`. `_END` stores the collected output in `result.captures` (and in the output folder when `save_to_disk=True`) and closes the block. Multiple command lines are allowed between `_CAPTURE` and `_END`.

Output from each command in the block is appended after the command completes. If `_EXPECT` is set, completion happens when the pattern matches or after `_WAIT` expires without a match (the command may be re-sent up to `_RETRY` times). If `_EXPECT` is not set, the command completes after `_WAIT` seconds with no retries.

### Normal lines

Any non-directive line is sent to the shell as a command (with trailing newline).

## Example: jump host, read and configure

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



## Defaults


| Setting                                                   | Value                                         |
| --------------------------------------------------------- | --------------------------------------------- |
| SSH port (`_PORT`)                                        | `22`                                          |
| Connect timeout (`_TIMEOUT`)                              | 10 seconds                                    |
| Command wait (`_WAIT`)                                    | 20 seconds                                    |
| Command retry (`_RETRY`)                                  | 1 attempt                                     |
| Error fail (`_ERRORFAIL`)                                 | `off` until a non-empty `_ERROR:` pattern is set (then `on`; cleared by empty `_ERROR:`) |
| Expect / error pattern                                    | empty (disabled)                              |
| Poll interval (internal)                                  | 0.5 seconds                                   |
| Shell size (`_SHELL`)                                     | 240 × 72                                      |
| Recv buffer (`_RECV_BUFFER`)                              | 65535 bytes                                   |
| Recv encoding (`_ENCODING`)                               | `utf-8` (invalid bytes ignored)              |
| SSH keepalive (internal)                                  | 60 seconds                                    |
| Host key policy (`host_key_policy` / `--host-key-policy`) | `auto` (`reject` = only system `known_hosts`) |




## Logging

Logging is configured automatically on each call.


| API                                                        | Log file location                                                      |
| ---------------------------------------------------------- | ---------------------------------------------------------------------- |
| `ssht()` (`save_to_disk=False`)                            | `sshtemp.log` in the current working directory                         |
| `ssht(..., save_to_disk=True)` / `sshtemp my_template.txt` | `sshtemp.log` inside the run output folder (`output_YYYYMMDD-HHMMSS/`) |


- Default (`verbose=False`): console and log file show **WARNING** and **ERROR** only
- `verbose=True`: also logs **INFO** for each command, capture, `expect_matched` / `expect_not_matched`, and `if_then_matched` (with output preview on expect miss, wait/retry)
- Log file lines are prefixed with a UTC ISO 8601 timestamp (`2026-08-22T19:17:03.456Z`); console/stderr lines are not
- `log_id="myhost2"` or `_LOGID:` in the template: prefixes every line, e.g. `ERROR:myhost2:sshtemp.engine:sshtemp.run_failed ...`
- Template validation errors: one `ERROR` log line per issue (`sshtemp.template_validation_error`)
- Invalid `host_key_policy`: one `ERROR` line (`sshtemp.invalid_host_key_policy`) before the run starts
- Non-fatal `_ERROR` matches (`_ERRORFAIL: off`) and template validation warnings (for example missing `_EXPECT`): logged immediately as `sshtemp.warning` with the current `log_id` / `host` when they occur

The first `ssht()` call in a process overwrites `sshtemp.log` in the cwd; later `ssht()` calls in the same process append to it. Each `save_to_disk=True` run creates a new output folder with its own `sshtemp.log`.

Example:

```text
2026-08-22T19:17:03.456Z ERROR:sshtemp.engine:sshtemp.template_validation_error error=line 3: missing _HOST before commands
2026-08-22T19:17:05.123Z ERROR:myhost2:sshtemp.engine:sshtemp.run_failed host=10.0.0.1 error=expect pattern ...
WARNING:10.1.1.1:sshtemp.engine:sshtemp.warning host=10.1.1.1 error=error pattern "..." matched ...
WARNING:myhost2:sshtemp.engine:sshtemp.warning error=no _EXPECT set; each command waits up to 20s (default _WAIT) with no prompt detection
INFO:myhost2:sshtemp.engine:sshtemp.command_sent template_line=12 command=show version wait_seconds=20
INFO:myhost2:sshtemp.engine:sshtemp.if_then_matched template_line=13 command=ssh ... pattern=.*assword:
```

## Secrets

Keep passwords and keys out of git. Use two patterns depending on how you run sshtemp:

### CLI — `${VAR}` in `.txt` templates

```text
_PASSWORD: ${jump_server_password}
...
${nokia_password}
```

```bash
export jump_server_password='clab'
export nokia_password='NokiaSros1!'
sshtemp router.txt
```

Placeholder names follow the usual shell environment variable rules: start with a letter or `_`, then letters, digits, and `_` only (for example `nokia_password`, `JUMP_PASSWORD`). Names with `-` or other characters are **not** substituted — use underscores instead. `${VAR}` inside `_#` comment lines is **not** substituted (documentation only).

If a `${VAR}` is not in the environment, the CLI prompts for it (masked input). On **Linux**, the placeholder name must match the environment variable exactly (case-sensitive). On **Windows**, lookup is case-insensitive (for example `${nokia_password}` matches `NOKIA_PASSWORD`). `sshtemp --check` does **not** read or validate environment variables — `${VAR}` placeholders are accepted as-is.

See `examples/03-other-jumpserver-nokia_sros-cli.txt`.

### Python — `os.environ` in template strings

`ssht()` does not expand `${VAR}`. Pass secrets in the template string:

```python
import os
from sshtemp import ssht

password = os.environ["NOKIA_PASSWORD"]

result = ssht("""
_HOST: 10.0.0.1
_USERNAME: admin
_PASSWORD: """ + password + """
_EXPECT: #\s*$
show version
""", save_to_disk=True)
```

See `examples/03-other-jumpserver-nokia_sros.py` for a multi-device jump-host flow.

## Security

sshtemp is a thin Paramiko wrapper around your templates. **Trust, credentials, and network safety are your responsibility.**


| Topic                 | Behavior                                                                                                                                                                                                                                                                                                                                                                                                      |
| --------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| **Host keys**         | Default `host_key_policy="auto"`: unknown SSH host keys are accepted (`AutoAddPolicy`). System `known_hosts` entries are still checked when present. Use `host_key_policy="reject"` (Python) or `--host-key-policy reject` (CLI) to refuse unknown hosts — ensure the jump host is in system `known_hosts` first. Applies only to the Paramiko connect (`_HOST:`), not nested `ssh` commands in the template. |
| **Credentials**       | Put passwords in templates only if you accept that risk. Prefer `${VAR}` on the CLI or `os.environ` in Python (see [Secrets](#secrets)). Empty `_PASSWORD:` lets Paramiko try key/agent auth like a normal SSH client.                                                                                                                                                                                        |
| **Nested** `ssh`      | Commands such as `ssh -o StrictHostKeyChecking=no ...` in the template are **your** choice; sshtemp does not rewrite them.                                                                                                                                                                                                                                                                                    |
| **MITM / path trust** | If the network path is untrusted, use `host_key_policy="reject"`, VPN, jump hosts, or verified host keys on every SSH layer you control.                                                                                                                                                                                                                                                                      |




## Parallel execution (advanced)

Sequential `ssht()` behavior is unchanged. This section is for concurrent runs over multiple `_HOST` sections.

**Status:** `ssht_parallel()` is available from Python and the CLI (`-p N` / `--parallel N`).

### Logging

| API | Log file location |
| --- | ----------------- |
| `ssht_parallel(..., save_to_disk=True)` / `sshtemp -p N template.txt` | One shared `sshtemp.log` in the run output folder |

The orchestrator configures logging once. Each worker runs `ssht(..., manage_logging=False)` and writes to the same log file. Use `_LOGID:` per section so `INFO` / `WARNING` / `ERROR` lines are easy to filter (for example `INFO:10.100.100.3:...`).

### `ssht_parallel()`

```python
from sshtemp import ssht_parallel

result = ssht_parallel(
    template,
    max_workers=4,
    save_to_disk=True,
)
if result.success:
    for section_result in result.results:
        print(section_result.host, section_result.captures)
else:
    print(result.error)
    if result.failed_section_index is not None:
        failed = result.results[result.failed_section_index]
        print(failed.error)
```

```python
ssht_parallel(
    template,
    max_workers: int,
    directive_prefix="_",
    *,
    verbose=False,
    save_to_disk=False,
    output_dir=None,
    host_key_policy="auto",
) -> SshTempParallelResult
```

`max_workers` is required and must be `>= 1`. It limits how many sections run at the same time.

**`ssht_parallel()` never raises.** Check `result.success`, then read `result.results` (one `SshTempResult` per `_HOST` section, in template order). Warnings and captures stay on each section result (`result.results[i].warnings`, `result.results[i].captures`). There is no merged capture dict at the top level.

| Field | Description |
| ----- | ----------- |
| `success` | `True` only when every section succeeded |
| `results` | `list[SshTempResult]` in `_HOST` section order |
| `output_dir` | Set when `save_to_disk=True` — shared folder for all workers |
| `error` | Prep failure message, or summary of the first failed section |
| `failed_section_index` | 0-based index of the first failed section; `None` when `success` is `True` |

All sections are started. One failure does not cancel the others.

With `save_to_disk=True`, workers share one output folder and one `sshtemp.log`. Use unique capture file names per device (for example `backup_config_{device}.txt`) when sections might write the same path. Use `_LOGID:` per section to distinguish devices in log lines.

### Template preparation

`ssht_parallel()` splits the template on each `_HOST:` line and injects connect stickies into later sections before workers start.

1. Split the template at each `_HOST:` line (one section per host block).
2. For each section after the first, insert missing sticky lines **immediately after that section's `_HOST:`** from the accumulated state of earlier sections.

**Injected when missing in a later section:** `_USERNAME`, `_PASSWORD`, `_PORT`, `_TIMEOUT`, `_SHELL`, `_RECV_BUFFER`, `_ENCODING`, `_EXPECT`.

**Not injected (differs from sequential `ssht()`):**

| Directive | Parallel behavior |
| --------- | ----------------- |
| `_LOGID` | Add per section when you need device identity in logs |
| `_ERROR` / `_ERRORFAIL` | Not carried; wrap configure blocks per section: `_ERROR:…` … `_ERROR:` |
| `_WAIT` / `_RETRY` | Not carried; set per section if needed |
| `_IF` / `_THEN` | Stay where you write them; not auto-copied |

Each section must have balanced `_CAPTURE` / `_END` pairs (no open capture at a section boundary).

For the default single-template workflow, ignore this section.

## Tests

```bash
python -m unittest discover -s tests -v
```

