"""Security and cryptography utilities for Vaani Desktop Tracker."""

from .dpapi import (
    DPAPIError,
    DPAPIDecryptionError,
    DPAPIEncryptionError,
    is_dpapi_available,
    protect_token,
    unprotect_token,
    is_protected_token,
    set_dpapi_test_backend,
    reset_dpapi_test_backend,
)

__all__ = [
    "DPAPIError",
    "DPAPIDecryptionError",
    "DPAPIEncryptionError",
    "is_dpapi_available",
    "protect_token",
    "unprotect_token",
    "is_protected_token",
    "set_dpapi_test_backend",
    "reset_dpapi_test_backend",
]

