import os
import re
import sys
from collections.abc import Callable
from getpass import getpass

# POSIX-style names: letter or underscore, then letters, digits, underscores.
_ENV_VAR_PATTERN = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


def _is_comment_line(line: str, directive_prefix: str = "_") -> bool:
    return line.startswith(directive_prefix + "#")


def _env_lookup(env: dict[str, str], name: str) -> str | None:
    """Return a non-empty environment value for name, or None if missing."""
    value = env.get(name)
    if value:
        return value
    if sys.platform != "win32":
        return None
    name_lower = name.lower()
    for key, val in env.items():
        if key.lower() == name_lower and val:
            return val
    return None


def find_env_var_names(
    template_text: str, directive_prefix: str = "_"
) -> list[str]:
    """Return unique ${VAR} names in first-seen order (skips _# comment lines)."""
    seen: set[str] = set()
    names: list[str] = []
    for line in template_text.splitlines():
        if _is_comment_line(line, directive_prefix):
            continue
        for match in _ENV_VAR_PATTERN.finditer(line):
            name = match.group(1)
            if name not in seen:
                seen.add(name)
                names.append(name)
    return names


def substitute_env_vars(
    template_text: str,
    *,
    environ: dict[str, str] | None = None,
    prompt_missing: bool = False,
    password_prompt: Callable[[str], str] = getpass,
    directive_prefix: str = "_",
) -> tuple[str, str | None]:
    """Replace ${VAR} placeholders using environment variables.

    Returns (rendered_text, error_message). error_message is set when a
    required variable is missing and prompt_missing is False, or when a
    prompted value is empty. Placeholders on ``_#`` comment lines are ignored.
    """
    env = dict(os.environ if environ is None else environ)

    for name in find_env_var_names(template_text, directive_prefix):
        if _env_lookup(env, name):
            continue
        if prompt_missing:
            value = password_prompt(f"{name}: ")
            if not value:
                return template_text, f"missing environment variable: {name}"
            env[name] = value
        else:
            return template_text, f"missing environment variable: {name}"

    def _replace(match: re.Match[str]) -> str:
        name = match.group(1)
        value = _env_lookup(env, name)
        if value is None:
            return match.group(0)
        return value

    rendered_lines: list[str] = []
    for line in template_text.splitlines(keepends=True):
        if _is_comment_line(line.rstrip("\r\n"), directive_prefix):
            rendered_lines.append(line)
        else:
            rendered_lines.append(_ENV_VAR_PATTERN.sub(_replace, line))
    return "".join(rendered_lines), None
