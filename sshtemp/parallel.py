"""Run multiple ``_HOST`` template sections concurrently."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from sshtemp.engine import (
    DEFAULT_HOST_KEY_POLICY,
    _format_write_error,
    _prepare_output_dir,
    _resolve_template_text,
    ssht,
)
from sshtemp.logging_config import LOG_FILE_NAME, configure_logging, shutdown_logging
from sshtemp.models import SshTempParallelResult, SshTempResult
from sshtemp.template_sections import (
    TemplateSectionsError,
    prepare_parallel_template_sections,
)


def _invalid_max_workers_message(max_workers: int) -> str | None:
    if max_workers < 1:
        return f"max_workers must be >= 1, got {max_workers!r}"
    return None


def _first_failed_section_index(results: list[SshTempResult]) -> int | None:
    for index, result in enumerate(results):
        if not result.success:
            return index
    return None


def _summarize_first_failure(results: list[SshTempResult]) -> str | None:
    index = _first_failed_section_index(results)
    if index is None:
        return None
    result = results[index]
    message = result.error or "run failed"
    return f"section {index + 1} failed: {message}"


def ssht_parallel(
    command_temp_text: str | Path,
    max_workers: int,
    directive_prefix: str = "_",
    *,
    verbose: bool = False,
    save_to_disk: bool = False,
    output_dir: str | Path | None = None,
    host_key_policy: str = DEFAULT_HOST_KEY_POLICY,
) -> SshTempParallelResult:
    """Run each ``_HOST`` section in parallel and return one result per section.

    ``ssht_parallel()`` never raises. Check ``result.success`` and
    ``result.results``. All sections are started; failures do not cancel others.
    """
    max_workers_issue = _invalid_max_workers_message(max_workers)
    if max_workers_issue:
        return SshTempParallelResult(success=False, error=max_workers_issue)

    template_text, load_error = _resolve_template_text(command_temp_text)
    if load_error:
        return SshTempParallelResult(success=False, error=load_error)

    try:
        sections = prepare_parallel_template_sections(
            template_text,
            directive_prefix=directive_prefix,
        )
    except TemplateSectionsError as exc:
        return SshTempParallelResult(success=False, error=str(exc))

    out_dir: Path | None = None
    try:
        try:
            if save_to_disk:
                out_dir = _prepare_output_dir(output_dir)
                configure_logging(
                    verbose=verbose,
                    log_file=out_dir / LOG_FILE_NAME,
                    append=False,
                )
            else:
                configure_logging(verbose=verbose)
        except OSError as exc:
            return SshTempParallelResult(
                success=False,
                output_dir=out_dir,
                error=_format_write_error(exc),
            )

        def run_section(section: str) -> SshTempResult:
            return ssht(
                section,
                directive_prefix=directive_prefix,
                verbose=verbose,
                save_to_disk=save_to_disk,
                output_dir=out_dir,
                host_key_policy=host_key_policy,
                manage_logging=False,
            )

        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = [
                executor.submit(run_section, section) for section in sections
            ]
            results = [future.result() for future in futures]

        success = all(result.success for result in results)
        failed_index = _first_failed_section_index(results)
        error = _summarize_first_failure(results) if not success else None

        return SshTempParallelResult(
            success=success,
            results=results,
            output_dir=out_dir,
            error=error,
            failed_section_index=failed_index,
        )
    finally:
        shutdown_logging()
