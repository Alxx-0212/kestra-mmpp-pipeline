"""Previous-format FinPay acquisition entrypoints."""

from .acquisition import acquire_legacy_day, expand_legacy_range, verify_legacy_artifact

__all__ = ["acquire_legacy_day", "expand_legacy_range", "verify_legacy_artifact"]
