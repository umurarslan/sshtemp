import codecs
import re

from sshtemp.exceptions import SshTempValidationError
from sshtemp.models import TemplateValidationResult

DEFAULT_WAIT_SECONDS = 20  # shared with engine default _WAIT

_PLACEHOLDER_PATTERN = re.compile(r"\$\{([^}]*)\}")
_VALID_PLACEHOLDER_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

_DIRECTIVES = (
    "HOST:",
    "USERNAME:",
    "PASSWORD:",
    "PORT:",
    "TIMEOUT:",
    "LOGID:",
    "SHELL:",
    "RECV_BUFFER:",
    "ENCODING:",
    "IF:",
    "THEN:",
    "ENTER",
    "EXPECT:",
    "ERROR:",
    "ERRORFAIL:",
    "WAIT:",
    "RETRY:",
    "SLEEP:",
    "CAPTURE",
    "END",
)


def validate_directive_prefix(directive_prefix: str) -> str | None:
    if not directive_prefix:
        return "directive_prefix must not be empty"
    return None


def parse_shell_size(raw: str) -> tuple[int, int]:
    match = re.fullmatch(r"(\d+)[xX](\d+)", raw.strip())
    if not match:
        raise ValueError("must be WIDTHxHEIGHT, e.g. 240x72")
    width = int(match.group(1))
    height = int(match.group(2))
    if width < 1 or height < 1:
        raise ValueError("width and height must be >= 1")
    return width, height


def parse_encoding(raw: str) -> str:
    value = raw.strip()
    if not value:
        raise ValueError("value is empty")
    try:
        codecs.lookup(value)
    except LookupError as exc:
        raise ValueError(f"unknown encoding: {value!r}") from exc
    return value


def is_directive_line(line: str, directive_prefix: str) -> bool:
    if line.startswith(directive_prefix + "#"):
        return True
    return any(line.startswith(directive_prefix + name) for name in _DIRECTIVES)


def _validate_placeholders(
    line_number: int,
    line: str,
    directive_prefix: str,
    issues: list[str],
) -> None:
    if line.startswith(directive_prefix + "#"):
        return
    for match in _PLACEHOLDER_PATTERN.finditer(line):
        name = match.group(1)
        if _VALID_PLACEHOLDER_NAME.fullmatch(name):
            continue
        issues.append(
            f"line {line_number}: invalid placeholder ${{{name}}}: "
            "name must start with a letter or underscore and contain only "
            "letters, digits, and underscores"
        )


def validate_template(
    command_temp_text: str,
    directive_prefix: str = "_",
    *,
    raise_on_error: bool = False,
) -> TemplateValidationResult:
    """Validate template syntax without opening an SSH connection."""
    prefix_issue = validate_directive_prefix(directive_prefix)
    if prefix_issue:
        result = TemplateValidationResult(valid=False, issues=[prefix_issue])
        if raise_on_error:
            raise SshTempValidationError(
                "template validation failed",
                [prefix_issue],
            )
        return result

    issues: list[str] = []
    warnings: list[str] = []
    template_lines = command_temp_text.splitlines()

    host_set = False
    username_set = False
    pending_if = False
    capturing = False
    capture_name = ""
    saw_command = False
    expect_pattern = ""
    error_pattern = ""

    for line_number, line in enumerate(template_lines, start=1):
        if not line.strip():
            continue
        if line.startswith(directive_prefix + "#"):
            continue

        _validate_placeholders(line_number, line, directive_prefix, issues)

        if line.startswith(directive_prefix + "HOST:"):
            host_set = bool(line.removeprefix(directive_prefix + "HOST:").strip())
            continue
        if line.startswith(directive_prefix + "USERNAME:"):
            username_set = bool(
                line.removeprefix(directive_prefix + "USERNAME:").strip()
            )
            continue
        if line.startswith(directive_prefix + "TIMEOUT:"):
            continue
        if line.startswith(directive_prefix + "LOGID:"):
            if not line.removeprefix(directive_prefix + "LOGID:").strip():
                issues.append(
                    f"line {line_number}: {directive_prefix}LOGID value is empty"
                )
            continue
        if line.startswith(directive_prefix + "SHELL:"):
            raw = line.removeprefix(directive_prefix + "SHELL:").strip()
            try:
                parse_shell_size(raw)
            except ValueError as exc:
                issues.append(
                    f"line {line_number}: invalid {directive_prefix}SHELL: {exc}"
                )
            continue
        if line.startswith(directive_prefix + "RECV_BUFFER:"):
            raw = line.removeprefix(directive_prefix + "RECV_BUFFER:").strip()
            try:
                if int(raw) < 1:
                    issues.append(
                        f"line {line_number}: {directive_prefix}RECV_BUFFER must be >= 1"
                    )
            except ValueError:
                issues.append(
                    f"line {line_number}: {directive_prefix}RECV_BUFFER must be "
                    f"an integer, got {raw!r}"
                )
            continue
        if line.startswith(directive_prefix + "ENCODING:"):
            raw = line.removeprefix(directive_prefix + "ENCODING:").strip()
            try:
                parse_encoding(raw)
            except ValueError as exc:
                issues.append(
                    f"line {line_number}: invalid {directive_prefix}ENCODING: {exc}"
                )
            continue
        if line.startswith(directive_prefix + "IF:"):
            if pending_if:
                issues.append(
                    f"line {line_number}: missing {directive_prefix}THEN for "
                    f"previous {directive_prefix}IF"
                )
            pending_if = True
            continue
        if line.startswith(directive_prefix + "THEN:"):
            if not pending_if:
                issues.append(
                    f"line {line_number}: {directive_prefix}THEN without "
                    f"{directive_prefix}IF"
                )
            else:
                pending_if = False
            continue
        if line.startswith(directive_prefix + "EXPECT:"):
            expect_pattern = line.removeprefix(directive_prefix + "EXPECT:").lstrip()
            if expect_pattern:
                try:
                    re.compile(expect_pattern)
                except re.error as exc:
                    issues.append(
                        f"line {line_number}: invalid {directive_prefix}EXPECT "
                        f"regex: {exc}"
                    )
            continue
        if line.startswith(directive_prefix + "ERROR:"):
            error_pattern = line.removeprefix(directive_prefix + "ERROR:").lstrip()
            if error_pattern:
                try:
                    re.compile(error_pattern)
                except re.error as exc:
                    issues.append(
                        f"line {line_number}: invalid {directive_prefix}ERROR "
                        f"regex: {exc}"
                    )
            continue
        if line.startswith(directive_prefix + "ERRORFAIL:"):
            value = line.removeprefix(directive_prefix + "ERRORFAIL:").strip().lower()
            if value not in ("on", "off"):
                issues.append(
                    f"line {line_number}: {directive_prefix}ERRORFAIL must be "
                    f"'on' or 'off', got {value!r}"
                )
            continue
        if line.startswith(directive_prefix + "WAIT:"):
            raw = line.removeprefix(directive_prefix + "WAIT:").strip()
            try:
                if int(raw) < 1:
                    issues.append(
                        f"line {line_number}: {directive_prefix}WAIT must be >= 1"
                    )
            except ValueError:
                issues.append(
                    f"line {line_number}: {directive_prefix}WAIT must be an "
                    f"integer, got {raw!r}"
                )
            continue
        if line.startswith(directive_prefix + "RETRY:"):
            raw = line.removeprefix(directive_prefix + "RETRY:").strip()
            try:
                if int(raw) < 1:
                    issues.append(
                        f"line {line_number}: {directive_prefix}RETRY must be >= 1"
                    )
            except ValueError:
                issues.append(
                    f"line {line_number}: {directive_prefix}RETRY must be an "
                    f"integer, got {raw!r}"
                )
            continue
        if line.startswith(directive_prefix + "SLEEP:"):
            raw = line.removeprefix(directive_prefix + "SLEEP:").strip()
            try:
                if int(raw) < 0:
                    issues.append(
                        f"line {line_number}: {directive_prefix}SLEEP must be >= 0"
                    )
            except ValueError:
                issues.append(
                    f"line {line_number}: {directive_prefix}SLEEP must be an "
                    f"integer, got {raw!r}"
                )
            continue
        if line.startswith(directive_prefix + "CAPTURE"):
            if capturing:
                issues.append(
                    f"line {line_number}: nested {directive_prefix}CAPTURE without "
                    f"{directive_prefix}END"
                )
            capturing = True
            capture_name = ""
            if line.startswith(directive_prefix + "CAPTURE:"):
                capture_name = line.removeprefix(
                    directive_prefix + "CAPTURE:"
                ).strip()
                if not capture_name:
                    issues.append(
                        f"line {line_number}: {directive_prefix}CAPTURE name is empty"
                    )
            continue
        if line.startswith(directive_prefix + "END"):
            if not capturing:
                issues.append(
                    f"line {line_number}: {directive_prefix}END without "
                    f"{directive_prefix}CAPTURE"
                )
            else:
                capturing = False
                capture_name = ""
            continue

        if is_directive_line(line, directive_prefix):
            continue

        if line.startswith(directive_prefix):
            issues.append(f"line {line_number}: unknown directive {line!r}")
            continue

        saw_command = True
        if pending_if:
            issues.append(
                f"line {line_number}: {directive_prefix}IF without "
                f"{directive_prefix}THEN before command"
            )
            pending_if = False

    if pending_if:
        issues.append(f"missing {directive_prefix}THEN for trailing {directive_prefix}IF")
    if capturing:
        issues.append(
            f"unclosed {directive_prefix}CAPTURE block, missing {directive_prefix}END"
        )
    if saw_command and not host_set:
        issues.append(f"missing {directive_prefix}HOST before commands")
    if saw_command and not username_set:
        issues.append(f"missing {directive_prefix}USERNAME before commands")
    if saw_command and not expect_pattern:
        warnings.append(
            f"no {directive_prefix}EXPECT set; each command waits up to "
            f"{DEFAULT_WAIT_SECONDS}s (default {directive_prefix}WAIT) with no "
            "prompt detection"
        )

    result = TemplateValidationResult(
        valid=not issues,
        issues=issues,
        warnings=warnings,
    )
    if raise_on_error and issues:
        raise SshTempValidationError(
            "template validation failed",
            issues=issues,
        )
    return result
