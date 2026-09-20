# sshtemp — instructions for AI assistants

Follow this file when writing or editing **sshtemp** templates (`ssht()`, `ssht_parallel()`, `sshtemp` CLI). Human docs: `README.md`. Example templates: `examples/`.

Copy this file into any project that uses sshtemp, or tell the assistant to follow it.

Interactive PTY (`invoke_shell`), not `exec_command` / Netmiko. Python 3.11+. `ssht()` / `ssht_parallel()` **never raise** — check `result.success`.

## Workflow

1. Write a `.txt` template (CLI) or a raw Python string `r"""..."""` (regex needs the `r`).
2. Validate: `sshtemp --check path/to/template.txt` or `validate_template(text)`.
3. Fix syntax errors; missing `_EXPECT` is a warning, not a hard fail.
4. Do **not** open SSH unless the user explicitly asked to run it.
5. Keep secrets out of git: CLI `${VAR}` (prompted if missing); Python `os.environ` — `ssht()` does **not** expand `${VAR}`.

`--check` does not split `_HOST` sections. A file can pass `--check` and still fail `sshtemp -p N`.

## Directive rules

Names are **exact**. `_ENDING` is not `_END`. Unknown `_NAME` is a validation error.

| Connect (Paramiko only) | Sticky | Scoped / one-shot |
| --- | --- | --- |
| `_HOST:` `_USERNAME:` `_PASSWORD:` `_PORT:` `_TIMEOUT:` | `_LOGID:` `_SHELL:` `_SETTLE:` `_RECV_BUFFER:` `_ENCODING:` `_EXPECT:` `_ERROR:` `_ERRORFAIL:` | `_WAIT:` `_RETRY:` `_IF:` `_THEN:` `_SLEEP:` `_ENTER` `_CAPTURE` `_END` `_#` |

- `_HOST` is the Paramiko hop. Nested devices are a normal `ssh user@device` line. `_HOST` / `result.host` do not follow nested SSH.
- Only a new `_HOST:` reconnects. Changing user/password/port is stored until the next `_HOST:`.
- After `invoke_shell`, MOTD is read before the first command (not captured, not `_ERROR`-checked). Slow banner: `_SETTLE: N` (default `2`) before the first command.
- Set `_EXPECT:` to the prompt **before** commands you care about. Without it, each line waits full `_WAIT` (default 20s).
- `_EXPECT` / `_ERROR` / `_IF` regex search the **current command** buffer only.
- `_WAIT` / `_RETRY` apply to the next command, or to a whole `_CAPTURE` block if placed immediately before `_CAPTURE`.
- `_RETRY` **re-sends** the line. Use on `show` / dumps. Never `> 1` on configure, commit, or a password line.
- Non-empty `_ERROR:` turns `_ERRORFAIL` on. Warn-only: `_ERRORFAIL: off` **after** `_ERROR:`. Clear with empty `_ERROR:` after configure so later `show` lines are not matched.
- `_IF` / `_THEN` bind to the **next** command, then clear. Password prompt: `_IF: .*assword:` (colon) so `PreferredAuthentications=password` does not match.
- `_CAPTURE:name` … `_END`. Close before the next `_HOST:`. No `..` or absolute paths. Duplicate names **append**.
- Python regex in templates: always a raw string (`r"""_EXPECT: #\s*$"""`).

## Jump host

`_HOST` = jump box. Then `_IF`/`_THEN` immediately before `ssh`:

```text
_HOST: 172.29.112.112
_USERNAME: clab
_PASSWORD: ${JUMP_PASSWORD}
_PORT: 2222
_EXPECT: #\s*$

_IF: continue connecting
_THEN: yes
_IF: .*assword:
_THEN: ${DEVICE_PASSWORD}
ssh -o PubkeyAuthentication=no -o PreferredAuthentications=password -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null admin@10.0.0.3
environment no more
```

Nokia classic CLI: `_EXPECT: #\s*$` (a line that is only `#` also matches). Paging off: `environment no more`. Large dump: `_WAIT: 120` then `_RETRY: 2` immediately before `_CAPTURE`.

## Python vs CLI

```python
from sshtemp import ssht

result = ssht(r"""
_HOST: 10.0.0.1
_USERNAME: admin
_PASSWORD: secret
_EXPECT: #\s*$
_CAPTURE:show_version
show version
_END
""")
if result.success:
    print(result.captures["show_version"])
else:
    print(result.error)
```

- CLI always writes `output_*/`. Python writes files only if `save_to_disk=True`.
- Default `host_key_policy` is `auto` (Paramiko `_HOST` only). Nested `ssh` options are the template’s problem.
- `ssht_parallel(template, max_workers, ...)` — required `max_workers >= 1`. No `log_id` (use `_LOGID:` per section). No merged capture dict.

## Parallel (`-p` / `ssht_parallel`)

Each section starts at `_HOST:`. Injected when missing: `_USERNAME` `_PASSWORD` `_PORT` `_TIMEOUT` `_SHELL` `_SETTLE` `_RECV_BUFFER` `_ENCODING` `_EXPECT`.

**Not** injected: `_LOGID`, `_ERROR`/`_ERRORFAIL`, `_WAIT`/`_RETRY`, `_IF`/`_THEN`. Repeat configure `_ERROR:` … `_ERROR:` in **each** section. Unique capture names and `_LOGID:` per device. Nothing except comments/blank lines before the first `_HOST:`.

## Do not

- Invent directives or treat prefix-matches as valid (`_ENDING`).
- Put `_CAPTURE` around the `ssh` login (MOTD / password prompts leak in).
- Leave `_ERROR:` sticky across devices or after configure.
- Run live SSH, print passwords, or skip `--check` when only asked to write a template.
