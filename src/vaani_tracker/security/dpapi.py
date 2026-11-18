"""Windows Data Protection API (DPAPI) token protection for Vaani Desktop Tracker.

Encrypts and decrypts sensitive authentication bearer tokens using Windows DPAPI
with user-scope credentials. Tokens are bound to the specific Windows user profile
and cannot be decrypted by other user accounts on the same workstation.

Format of protected tokens on disk:
    "dpapi:<base64-encoded-ciphertext>"

Technical notes on Windows profile changes:
    DPAPI keys are derived from the Windows user's password and stored in
    %APPDATA%\\Microsoft\\Protect\\<User-SID>. If a Windows user profile is deleted,
    recreated, or moved to another domain without roaming profile keys,
    CryptUnprotectData will fail with NTE_BAD_KEY_STATE or SEC_E_DECRYPT_FAILURE.
    In that scenario, this module raises DPAPIDecryptionError so the caller can
    safely mark the workstation as unauthenticated and prompt re-pairing
    WITHOUT losing any local offline queued activity buckets.
"""

import base64
import ctypes
import logging
import platform
from typing import Callable, Optional, Tuple

logger = logging.getLogger(__name__)

# Constants
DPAPI_PREFIX = "dpapi:"
CRYPTPROTECT_UI_FORBIDDEN = 0x01

# Custom Exceptions
class DPAPIError(Exception):
    """Base exception for DPAPI operations."""
    pass


class DPAPIEncryptionError(DPAPIError):
    """Raised when token encryption fails."""
    pass


class DPAPIDecryptionError(DPAPIError):
    """Raised when protected token decryption fails."""
    pass


# Windows ctypes definitions
_is_windows = platform.system() == "Windows"
_crypt32 = None
_kernel32 = None

if _is_windows:
    try:
        from ctypes import wintypes

        class DATA_BLOB(ctypes.Structure):
            _fields_ = [
                ("cbData", wintypes.DWORD),
                ("pbData", ctypes.POINTER(ctypes.c_byte)),
            ]

        _crypt32 = ctypes.windll.crypt32
        _kernel32 = ctypes.windll.kernel32

        # CryptProtectData prototype
        _crypt32.CryptProtectData.argtypes = [
            ctypes.POINTER(DATA_BLOB),          # pDataIn
            wintypes.LPCWSTR,                   # szDataDescr
            ctypes.POINTER(DATA_BLOB),          # pOptionalEntropy
            ctypes.c_void_p,                    # pvReserved
            ctypes.c_void_p,                    # pPromptStruct
            wintypes.DWORD,                     # dwFlags
            ctypes.POINTER(DATA_BLOB),          # pDataOut
        ]
        _crypt32.CryptProtectData.restype = wintypes.BOOL

        # CryptUnprotectData prototype
        _crypt32.CryptUnprotectData.argtypes = [
            ctypes.POINTER(DATA_BLOB),          # pDataIn
            ctypes.POINTER(wintypes.LPWSTR),    # ppszDataDescr
            ctypes.POINTER(DATA_BLOB),          # pOptionalEntropy
            ctypes.c_void_p,                    # pvReserved
            ctypes.c_void_p,                    # pPromptStruct
            wintypes.DWORD,                     # dwFlags
            ctypes.POINTER(DATA_BLOB),          # pDataOut
        ]
        _crypt32.CryptUnprotectData.restype = wintypes.BOOL

        # kernel32.LocalFree prototype (mandatory on 64-bit Windows to avoid pointer truncation)
        _kernel32.LocalFree.argtypes = [ctypes.c_void_p]
        _kernel32.LocalFree.restype = ctypes.c_void_p

    except Exception as e:
        logger.warning(f"Failed to initialize Windows DPAPI ctypes bindings: {e}")
        _crypt32 = None
        _kernel32 = None

# Optional test backend for non-Windows platforms or test mocking
_test_backend: Optional[Tuple[Callable[[bytes], bytes], Callable[[bytes], bytes]]] = None


def set_dpapi_test_backend(
    encrypt_fn: Callable[[bytes], bytes],
    decrypt_fn: Callable[[bytes], bytes],
) -> None:
    """Configures explicit encryption/decryption callbacks for test verification.

    Safety guard: strictly disallowed in production. Only allowed if a test
    runner or test environment variable is detected.
    """
    import os
    import sys
    is_test = (
        bool(os.environ.get("PYTEST_CURRENT_TEST"))
        or os.environ.get("VAANI_TEST_MODE") == "1"
        or os.environ.get("VAANI_ENV") == "test"
        or "pytest" in sys.modules
        or "unittest" in sys.modules
    )
    if not is_test:
        raise RuntimeError(
            "Security violation: Test DPAPI backend cannot be enabled outside of a test environment."
        )
    global _test_backend
    _test_backend = (encrypt_fn, decrypt_fn)


def reset_dpapi_test_backend() -> None:
    """Clears any test backend adapter."""
    global _test_backend
    _test_backend = None


def is_dpapi_available() -> bool:
    """Returns True if native Windows DPAPI or a test backend is available."""
    if _test_backend is not None:
        return True
    return _is_windows and _crypt32 is not None and _kernel32 is not None


def is_protected_token(value: Optional[str]) -> bool:
    """Checks whether a token string has the DPAPI protected prefix."""
    if not value or not isinstance(value, str):
        return False
    return value.startswith(DPAPI_PREFIX)


def protect_token(plaintext_token: str) -> str:
    """Encrypts a plaintext token using Windows DPAPI user scope.

    Returns:
        Formatted string: "dpapi:<base64-ciphertext>"

    Raises:
        DPAPIEncryptionError: If encryption fails or DPAPI is unavailable.
    """
    if not plaintext_token:
        raise DPAPIEncryptionError("Cannot protect empty or null token.")

    if is_protected_token(plaintext_token):
        # Already protected
        return plaintext_token

    raw_bytes = plaintext_token.encode("utf-8")

    # Use test backend if explicitly registered
    if _test_backend is not None:
        try:
            ciphertext = _test_backend[0](raw_bytes)
            b64 = base64.b64encode(ciphertext).decode("ascii")
            return f"{DPAPI_PREFIX}{b64}"
        except Exception as e:
            raise DPAPIEncryptionError(f"Test backend encryption failed: {type(e).__name__}") from e

    if not is_dpapi_available():
        raise DPAPIEncryptionError(
            "Windows DPAPI is not available on this platform. "
            "DPAPI token protection requires Windows with crypt32.dll."
        )

    try:
        in_blob = DATA_BLOB(
            len(raw_bytes),
            ctypes.cast(ctypes.create_string_buffer(raw_bytes), ctypes.POINTER(ctypes.c_byte)),
        )
        out_blob = DATA_BLOB()

        success = _crypt32.CryptProtectData(
            ctypes.byref(in_blob),
            "VaaniDeviceAuthToken",         # szDataDescr
            None,                           # pOptionalEntropy
            None,                           # pvReserved
            None,                           # pPromptStruct
            CRYPTPROTECT_UI_FORBIDDEN,      # dwFlags: user-scoped, no UI dialogs
            ctypes.byref(out_blob),
        )

        if not success:
            err_code = ctypes.GetLastError()
            raise DPAPIEncryptionError(f"CryptProtectData failed with Win32 error code {err_code}")

        try:
            encrypted_data = ctypes.string_at(out_blob.pbData, out_blob.cbData)
            b64 = base64.b64encode(encrypted_data).decode("ascii")
            return f"{DPAPI_PREFIX}{b64}"
        finally:
            _kernel32.LocalFree(out_blob.pbData)

    except DPAPIError:
        raise
    except Exception as e:
        raise DPAPIEncryptionError(f"Unexpected error during DPAPI encryption: {type(e).__name__}") from e


def unprotect_token(protected_token: str) -> str:
    """Decrypts a DPAPI-protected token into memory.

    Args:
        protected_token: String starting with "dpapi:<base64-ciphertext>"

    Returns:
        Decrypted plaintext token string.

    Raises:
        DPAPIDecryptionError: If the token is corrupted, wrong format, or decryption fails.
    """
    if not protected_token or not isinstance(protected_token, str):
        raise DPAPIDecryptionError("Cannot decrypt empty or null protected token.")

    if not is_protected_token(protected_token):
        raise DPAPIDecryptionError("Token string lacks required 'dpapi:' prefix.")

    b64_payload = protected_token[len(DPAPI_PREFIX):]
    try:
        ciphertext = base64.b64decode(b64_payload.encode("ascii"))
    except Exception as e:
        raise DPAPIDecryptionError("Protected token has invalid base64 encoding.") from e

    if not ciphertext:
        raise DPAPIDecryptionError("Protected token ciphertext is empty.")

    # Use test backend if explicitly registered
    if _test_backend is not None:
        try:
            plaintext = _test_backend[1](ciphertext)
            return plaintext.decode("utf-8")
        except Exception as e:
            raise DPAPIDecryptionError(f"Test backend decryption failed: {type(e).__name__}") from e

    if not is_dpapi_available():
        raise DPAPIDecryptionError(
            "Windows DPAPI is not available on this platform. "
            "DPAPI token decryption requires Windows with crypt32.dll."
        )

    try:
        in_blob = DATA_BLOB(
            len(ciphertext),
            ctypes.cast(ctypes.create_string_buffer(ciphertext), ctypes.POINTER(ctypes.c_byte)),
        )
        out_blob = DATA_BLOB()

        success = _crypt32.CryptUnprotectData(
            ctypes.byref(in_blob),
            None,                           # ppszDataDescr
            None,                           # pOptionalEntropy
            None,                           # pvReserved
            None,                           # pPromptStruct
            CRYPTPROTECT_UI_FORBIDDEN,      # dwFlags: no UI dialogs
            ctypes.byref(out_blob),
        )

        if not success:
            err_code = ctypes.GetLastError()
            raise DPAPIDecryptionError(
                f"CryptUnprotectData failed with Win32 error code {err_code}. "
                "The current Windows user profile may not have access to the encryption key."
            )

        try:
            decrypted_data = ctypes.string_at(out_blob.pbData, out_blob.cbData)
            return decrypted_data.decode("utf-8")
        finally:
            _kernel32.LocalFree(out_blob.pbData)

    except DPAPIError:
        raise
    except Exception as e:
        raise DPAPIDecryptionError(f"Unexpected error during DPAPI decryption: {type(e).__name__}") from e

