class SshTempError(Exception):
    """Base exception for sshtemp."""

    def __init__(
        self,
        message: str,
        *,
        host: str | None = None,
        template_line: int | None = None,
        command: str | None = None,
        capture_name: str | None = None,
    ) -> None:
        super().__init__(message)
        self.host = host
        self.template_line = template_line
        self.command = command
        self.capture_name = capture_name


class SshTempConnectError(SshTempError):
    """Raised when SSH connection or shell setup fails."""


class SshTempTimeoutError(SshTempError):
    """Raised when expect pattern is not matched within _WAIT."""


class SshTempErrorPattern(SshTempError):
    """Raised when _ERROR pattern matches and _ERRORFAIL is on."""


class SshTempTemplateError(SshTempError):
    """Raised when template structure is invalid at runtime."""


class SshTempValidationError(SshTempError):
    """Raised when validate_template finds blocking issues."""

    def __init__(
        self,
        message: str,
        issues: list[str],
        *,
        host: str | None = None,
    ) -> None:
        super().__init__(message, host=host)
        self.issues = issues
