"""sshtemp — template-driven SSH automation.

Public API: ``ssht()``, ``ssht_parallel()``, ``validate_template()``, and result models.
``ssht()`` and ``ssht_parallel()`` never raise — check ``result.success``. Exception
classes live in ``sshtemp.exceptions`` for internal/advanced use.
"""

__version__ = "2026.10.12"

from sshtemp.engine import ssht
from sshtemp.models import (
    SshTempParallelResult,
    SshTempResult,
    TemplateValidationResult,
)
from sshtemp.parallel import ssht_parallel
from sshtemp.validator import validate_template

__all__ = [
    "SshTempParallelResult",
    "SshTempResult",
    "TemplateValidationResult",
    "ssht",
    "ssht_parallel",
    "validate_template",
    "__version__",
]

