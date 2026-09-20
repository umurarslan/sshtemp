from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class SshTempCapture:
    name: str
    output: str


@dataclass
class SshTempRunContext:
    host: str | None = None
    log_id: str | None = None
    verbose: bool = False
    host_key_policy: str = "auto"
    warnings: list[str] = field(default_factory=list)
    partial_capture: SshTempCapture | None = None
    last_recv_text: str = ""


@dataclass
class SshTempResult:
    success: bool
    host: str | None = None
    log_id: str | None = None
    captures: dict[str, str] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    error: str | None = None
    output_dir: Path | None = None
    failed_line: int | None = None
    failed_command: str | None = None
    failed_capture_name: str | None = None


@dataclass
class SshTempParallelResult:
    success: bool
    results: list[SshTempResult] = field(default_factory=list)
    output_dir: Path | None = None
    error: str | None = None
    failed_section_index: int | None = None


@dataclass
class TemplateValidationResult:
    valid: bool
    issues: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
