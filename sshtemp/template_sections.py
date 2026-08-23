"""Split templates on ``_HOST:`` and expand sticky connect directives for parallel runs.

Used internally by ``ssht_parallel()`` before workers start. Single ``ssht()`` is unchanged.
"""

from __future__ import annotations

STICKY_INJECT_ORDER: tuple[str, ...] = (
    "USERNAME",
    "PASSWORD",
    "PORT",
    "TIMEOUT",
    "SHELL",
    "RECV_BUFFER",
    "ENCODING",
    "EXPECT",
)


class TemplateSectionsError(ValueError):
    """Raised when a template cannot be split or expanded for parallel execution."""


def split_template_sections(
    command_temp_text: str,
    directive_prefix: str = "_",
) -> list[str]:
    """Split *command_temp_text* into sections at each ``{prefix}HOST:`` line.

    Each section starts with its ``_HOST:`` line. Empty lines and ``_#`` comments
    before the first ``_HOST:`` are allowed; any other content before the first
    ``_HOST:`` raises ``TemplateSectionsError``.
    """
    if not directive_prefix:
        raise TemplateSectionsError("directive_prefix must not be empty")

    lines = command_temp_text.splitlines()
    host_marker = directive_prefix + "HOST:"
    host_indices = [index for index, line in enumerate(lines) if line.startswith(host_marker)]

    if not host_indices:
        raise TemplateSectionsError(f"template has no {directive_prefix}HOST directive")

    first_host = host_indices[0]
    for index in range(first_host):
        line = lines[index]
        if not line.strip():
            continue
        if line.startswith(directive_prefix + "#"):
            continue
        raise TemplateSectionsError(
            f"content before first {directive_prefix}HOST at line {index + 1}"
        )

    sections: list[str] = []
    for section_index, start in enumerate(host_indices):
        end = host_indices[section_index + 1] if section_index + 1 < len(host_indices) else len(lines)
        sections.append("\n".join(lines[start:end]))
    return sections


def expand_template_sections(
    sections: list[str],
    directive_prefix: str = "_",
) -> list[str]:
    """Inject missing sticky directives from earlier sections into later ones.

    For each section after the first, any directive in ``STICKY_INJECT_ORDER`` that
    appears in the accumulated state but not in that section is inserted immediately
    after the section's ``_HOST:`` line.

    ``_LOGID``, ``_ERROR`` / ``_ERRORFAIL``, ``_WAIT``, and ``_RETRY`` are never
    injected. Each section is validated for a closed ``_CAPTURE`` / ``_END`` pair.
    """
    if not directive_prefix:
        raise TemplateSectionsError("directive_prefix must not be empty")
    if not sections:
        return []

    carried: dict[str, str] = {}
    expanded: list[str] = []

    for section_index, section_text in enumerate(sections):
        lines = section_text.splitlines()
        if section_index > 0:
            lines = _inject_carried_stickies(lines, carried, directive_prefix)
        _validate_section_capture_closed(lines, directive_prefix, section_index + 1)
        expanded.append("\n".join(lines))
        carried.update(_sticky_lines_from_section(lines, directive_prefix))

    return expanded


def prepare_parallel_template_sections(
    command_temp_text: str,
    directive_prefix: str = "_",
) -> list[str]:
    """Split on ``_HOST:`` and expand sticky directives for parallel workers."""
    return expand_template_sections(
        split_template_sections(command_temp_text, directive_prefix),
        directive_prefix,
    )


def _directive_marker(directive_prefix: str, name: str) -> str:
    return directive_prefix + name + ":"


def _section_has_directive(
    section_lines: list[str],
    directive_prefix: str,
    name: str,
) -> bool:
    marker = _directive_marker(directive_prefix, name)
    return any(line.startswith(marker) for line in section_lines)


def _sticky_lines_from_section(
    section_lines: list[str],
    directive_prefix: str,
) -> dict[str, str]:
    stickies: dict[str, str] = {}
    for line in section_lines:
        if not line.strip():
            continue
        if line.startswith(directive_prefix + "#"):
            continue
        for name in STICKY_INJECT_ORDER:
            marker = _directive_marker(directive_prefix, name)
            if line.startswith(marker):
                stickies[name] = line
    return stickies


def _inject_carried_stickies(
    section_lines: list[str],
    carried: dict[str, str],
    directive_prefix: str,
) -> list[str]:
    host_marker = directive_prefix + "HOST:"
    host_index = next(
        (index for index, line in enumerate(section_lines) if line.startswith(host_marker)),
        None,
    )
    if host_index is None:
        raise TemplateSectionsError(f"section is missing {directive_prefix}HOST directive")

    inject_lines = [
        carried[name]
        for name in STICKY_INJECT_ORDER
        if name in carried
        and not _section_has_directive(section_lines, directive_prefix, name)
    ]
    if not inject_lines:
        return section_lines

    insert_at = host_index + 1
    return (
        section_lines[:insert_at]
        + inject_lines
        + section_lines[insert_at:]
    )


def _validate_section_capture_closed(
    section_lines: list[str],
    directive_prefix: str,
    section_number: int,
) -> None:
    capturing = False
    for line_number, line in enumerate(section_lines, start=1):
        if not line.strip():
            continue
        if line.startswith(directive_prefix + "#"):
            continue
        if line.startswith(directive_prefix + "CAPTURE"):
            if capturing:
                raise TemplateSectionsError(
                    f"section {section_number} line {line_number}: nested "
                    f"{directive_prefix}CAPTURE without {directive_prefix}END"
                )
            capturing = True
            continue
        if line.startswith(directive_prefix + "END"):
            if not capturing:
                raise TemplateSectionsError(
                    f"section {section_number} line {line_number}: "
                    f"{directive_prefix}END without {directive_prefix}CAPTURE"
                )
            capturing = False

    if capturing:
        raise TemplateSectionsError(
            f"section {section_number}: unclosed {directive_prefix}CAPTURE block, "
            f"missing {directive_prefix}END"
        )
