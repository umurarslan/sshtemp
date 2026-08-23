import argparse
import sys
from pathlib import Path

from sshtemp.engine import ssht
from sshtemp.env_subst import substitute_env_vars
from sshtemp.parallel import ssht_parallel
from sshtemp.validator import validate_template


def _print_warnings(warnings: list[str]) -> None:
    for warning in warnings:
        print(f"warning: {warning}", file=sys.stderr)


def _format_failure_location(result) -> str | None:
    parts: list[str] = []
    if result.failed_line is not None:
        parts.append(f"line {result.failed_line}")
    if result.failed_command:
        parts.append(f"command {result.failed_command!r}")
    if result.failed_capture_name:
        parts.append(f"capture {result.failed_capture_name!r}")
    if not parts:
        return None
    return "failed at " + "; ".join(parts)


def _emit_run_result(result) -> int:
    if result.warnings:
        print(f"{len(result.warnings)} warning(s):", file=sys.stderr)
        _print_warnings(result.warnings)
    if result.success:
        if result.output_dir is not None:
            print(f"output: {result.output_dir}")
        return 0
    if result.captures:
        names = ", ".join(sorted(result.captures))
        print(
            f"partial captures ({len(result.captures)}): {names}",
            file=sys.stderr,
        )
    if result.output_dir is not None:
        print(f"output: {result.output_dir}", file=sys.stderr)
    failure_location = _format_failure_location(result)
    if failure_location:
        print(failure_location, file=sys.stderr)
    print(result.error or "run failed", file=sys.stderr)
    return 1


def _emit_parallel_run_result(result) -> int:
    warning_lines: list[str] = []
    for index, section_result in enumerate(result.results):
        if not section_result.warnings:
            continue
        label = (
            section_result.log_id
            or section_result.host
            or f"section {index}"
        )
        for warning in section_result.warnings:
            warning_lines.append(f"[{label}] warning: {warning}")
    if warning_lines:
        print(f"{len(warning_lines)} warning(s):", file=sys.stderr)
        for line in warning_lines:
            print(line, file=sys.stderr)

    if result.success:
        if result.output_dir is not None:
            print(f"output: {result.output_dir}")
        return 0

    if result.output_dir is not None:
        print(f"output: {result.output_dir}", file=sys.stderr)
    if result.failed_section_index is not None and result.results:
        failed = result.results[result.failed_section_index]
        label = (
            failed.log_id
            or failed.host
            or f"section {result.failed_section_index}"
        )
        failure_location = _format_failure_location(failed)
        if failure_location:
            print(f"{label}: {failure_location}", file=sys.stderr)
        if failed.error:
            print(f"{label}: {failed.error}", file=sys.stderr)
    print(result.error or "run failed", file=sys.stderr)
    return 1


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        prog="sshtemp",
        description="Run an SSH template file and write captures to an output folder.",
    )
    parser.add_argument("template", nargs="?", help="Path to the template file")
    parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="Log INFO events (commands, captures, expect details)",
    )
    parser.add_argument(
        "-o",
        "--output-dir",
        metavar="DIR",
        help="Output folder for capture files and sshtemp.log",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="Validate template syntax only (no SSH, no ${VAR} substitution)",
    )
    parser.add_argument(
        "--host-key-policy",
        choices=["auto", "reject"],
        default="auto",
        help="SSH host key policy for Paramiko connect (default: auto)",
    )
    parser.add_argument(
        "-p",
        "--parallel",
        type=int,
        metavar="N",
        dest="max_workers",
        help="Run each _HOST section concurrently with at most N workers",
    )

    args = parser.parse_args(argv)

    if not args.template:
        parser.print_usage(sys.stderr)
        sys.exit(2)

    template_path = Path(args.template)
    if not template_path.is_file():
        print(f"template file not found: {template_path}", file=sys.stderr)
        sys.exit(1)

    template_text = template_path.read_text(encoding="utf-8")

    if args.check:
        validation = validate_template(template_text)
        if validation.warnings:
            _print_warnings(validation.warnings)
        if validation.valid:
            sys.exit(0)
        print("template validation failed:", file=sys.stderr)
        for issue in validation.issues:
            print(f"  {issue}", file=sys.stderr)
        sys.exit(1)

    rendered, subst_error = substitute_env_vars(
        template_text,
        prompt_missing=True,
    )
    if subst_error:
        print(subst_error, file=sys.stderr)
        sys.exit(1)

    if args.max_workers is not None:
        if args.max_workers < 1:
            print(
                f"max_workers must be >= 1, got {args.max_workers!r}",
                file=sys.stderr,
            )
            sys.exit(1)
        result = ssht_parallel(
            rendered,
            args.max_workers,
            verbose=args.verbose,
            save_to_disk=True,
            output_dir=args.output_dir,
            host_key_policy=args.host_key_policy,
        )
        sys.exit(_emit_parallel_run_result(result))

    result = ssht(
        rendered,
        verbose=args.verbose,
        save_to_disk=True,
        output_dir=args.output_dir,
        host_key_policy=args.host_key_policy,
    )

    sys.exit(_emit_run_result(result))


if __name__ == "__main__":
    main()
