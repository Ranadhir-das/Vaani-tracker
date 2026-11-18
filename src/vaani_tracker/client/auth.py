"""Device authentication and employee binding manager."""

import logging
from typing import Optional, Dict, Any

from ..config import TrackerConfig
from ..models import DeviceInfo
from ..storage.queue import ActivityQueue
from ..security.dpapi import (
    is_dpapi_available,
    protect_token,
    unprotect_token,
    is_protected_token,
    DPAPIDecryptionError,
    DPAPIEncryptionError,
)

logger = logging.getLogger(__name__)


class DeviceAuthManager:
    """Manages secure device registration tokens and employee identity binding."""

    def __init__(self, config: TrackerConfig, queue: ActivityQueue):
        self.config = config
        self.queue = queue
        self._cached_token: Optional[str] = None

    def get_token(self) -> Optional[str]:
        """Returns the active device authentication token, decrypting with DPAPI if protected.

        Fails closed: if DPAPI is unavailable, token is corrupted, or decryption fails,
        returns None and refuses to use plaintext.
        """
        if self._cached_token:
            return self._cached_token

        # Check SQLite metadata storage first
        raw_meta = self.queue.get_meta("auth_token")
        if raw_meta:
            if is_protected_token(raw_meta):
                try:
                    decrypted = unprotect_token(raw_meta)
                    self._cached_token = decrypted
                    return decrypted
                except DPAPIDecryptionError:
                    logger.warning(
                        "Device authentication token in metadata could not be decrypted with Windows DPAPI. "
                        "The Windows user profile encryption keys may have changed."
                    )
                    return None
            else:
                # Legacy plaintext token in SQLite meta: migrate to DPAPI protected form immediately
                legacy_plain = str(raw_meta)
                if is_dpapi_available():
                    try:
                        protected = protect_token(legacy_plain)
                        self.queue.set_meta("auth_token", protected)
                        self.config.auth_token = legacy_plain
                        self.config.save()
                        self._cached_token = legacy_plain
                        return legacy_plain
                    except Exception as e:
                        logger.warning(f"Could not migrate legacy metadata token to DPAPI: {type(e).__name__}")
                        return None
                else:
                    logger.warning("Windows DPAPI is unavailable; refusing to use legacy plaintext token from metadata.")
                    return None

        # Fallback to config file
        if self.config.auth_token:
            if is_protected_token(self.config.auth_token):
                try:
                    decrypted = unprotect_token(self.config.auth_token)
                    self._cached_token = decrypted
                    # Ensure SQLite metadata is in sync with protected form
                    self.queue.set_meta("auth_token", self.config.auth_token)
                    return decrypted
                except DPAPIDecryptionError:
                    logger.warning("Protected token in config.json could not be decrypted with DPAPI.")
                    return None
            elif is_dpapi_available():
                try:
                    protected = protect_token(self.config.auth_token)
                    self.queue.set_meta("auth_token", protected)
                    self._cached_token = self.config.auth_token
                    return self.config.auth_token
                except Exception:
                    return None
            else:
                logger.warning("Windows DPAPI is unavailable; refusing to use plaintext config token.")
                return None

        return None

    def set_token(self, token: str) -> None:
        """Saves the device authentication token with DPAPI encryption.

        Account switching safety:
        If an account was already BOUND, setting a new token invalidates the old employee binding
        in SQLite and config to prevent a new token from ever operating with an old employee identity.
        If an employee was just set in EMPLOYEE_PENDING state, completing with this token binds them.
        Fails closed: requires DPAPI encryption and never stores plaintext.
        """
        if not token or not isinstance(token, str):
            raise ValueError("Token must be a non-empty string.")

        if not is_dpapi_available():
            raise DPAPIEncryptionError("Windows DPAPI is not available; cannot securely persist token.")

        protected_token = protect_token(token)

        current_token = self.get_token()
        current_state = self.queue.get_meta("auth_state")
        pending_emp_id = self.queue.get_meta("employee_id")
        pending_emp_name = self.queue.get_meta("employee_name")
        pending_emp_email = self.queue.get_meta("employee_email")

        # If an employee was configured and waiting for token, OR if the same token is being re-saved:
        if (current_state == "EMPLOYEE_PENDING" and pending_emp_id is not None) or (
            current_state == "BOUND" and current_token == token and pending_emp_id is not None
        ):
            if hasattr(self.queue, "set_auth_state_atomic"):
                self.queue.set_auth_state_atomic(
                    auth_token_protected=protected_token,
                    employee_id=int(pending_emp_id),
                    employee_name=pending_emp_name,
                    employee_email=pending_emp_email,
                    auth_state="BOUND",
                )
            else:
                self.queue.set_meta("auth_token", protected_token)
                self.queue.set_meta("auth_state", "BOUND")

            self._cached_token = token
            self.config.auth_token = token
            self.config.registered_employee_id = int(pending_emp_id)
            self.config.registered_employee_name = pending_emp_name
            self.config.save()
        else:
            # New token or account switch from a prior BOUND state: invalidate employee binding
            if hasattr(self.queue, "set_auth_state_atomic"):
                self.queue.set_auth_state_atomic(
                    auth_token_protected=protected_token,
                    employee_id=None,
                    employee_name=None,
                    employee_email=None,
                    auth_state="TOKEN_ONLY",
                )
            else:
                self.queue.set_meta("auth_token", protected_token)
                self.queue.set_meta("auth_state", "TOKEN_ONLY")

            self._cached_token = token
            self.config.auth_token = token
            self.config.registered_employee_id = None
            self.config.registered_employee_name = None
            self.config.save()

    def bind_employee(self, employee_id: int, employee_name: str, employee_email: Optional[str] = None) -> None:
        """Binds the local device to the authenticated employee."""
        if not isinstance(employee_id, int) or employee_id <= 0:
            raise ValueError("employee_id must be a positive integer.")

        token_prot = self.queue.get_meta("auth_token")
        if not token_prot and self.config.auth_token:
            token_prot = self.config.auth_token

        current_token = self.get_token()
        if current_token:
            # Token already present: transition directly to BOUND
            if hasattr(self.queue, "set_auth_state_atomic"):
                self.queue.set_auth_state_atomic(
                    auth_token_protected=token_prot,
                    employee_id=employee_id,
                    employee_name=employee_name,
                    employee_email=employee_email,
                    auth_state="BOUND",
                )
            else:
                self.queue.set_meta("employee_id", employee_id)
                self.queue.set_meta("employee_name", employee_name)
                if employee_email:
                    self.queue.set_meta("employee_email", employee_email)
                self.queue.set_meta("auth_state", "BOUND")

            self.config.registered_employee_id = employee_id
            self.config.registered_employee_name = employee_name
            self.config.save()
        else:
            # Token not yet present: mark EMPLOYEE_PENDING (fails closed until token is provided)
            if hasattr(self.queue, "set_auth_state_atomic"):
                self.queue.set_auth_state_atomic(
                    auth_token_protected=None,
                    employee_id=employee_id,
                    employee_name=employee_name,
                    employee_email=employee_email,
                    auth_state="EMPLOYEE_PENDING",
                )
            else:
                self.queue.set_meta("employee_id", employee_id)
                self.queue.set_meta("employee_name", employee_name)
                if employee_email:
                    self.queue.set_meta("employee_email", employee_email)
                self.queue.set_meta("auth_state", "EMPLOYEE_PENDING")

            self.config.registered_employee_id = employee_id
            self.config.registered_employee_name = employee_name
            self.config.save()

        logger.info(f"Device bound to employee: {employee_name} (ID: {employee_id})")

    def get_authenticated_employee_id(self) -> Optional[int]:
        """Authoritative employee identity resolver based on authenticated binding.

        Enforces:
        1. Device must possess a valid, decryptable authentication token.
        2. SQLite metadata must have auth_state == "BOUND".
        3. SQLite metadata must contain an explicit positive employee_id.
        4. Config registered_employee_id must strictly match SQLite employee_id.
        5. Incomplete, partial, or mismatched registration fails closed immediately.
        """
        token = self.get_token()
        if not token:
            return None

        # Verify auth_state is explicitly BOUND (fails closed if None, missing, or unknown)
        auth_state = self.queue.get_meta("auth_state")
        if auth_state != "BOUND":
            logger.debug(f"Auth state is '{auth_state}', expected 'BOUND'; failing closed.")
            return None

        raw_meta = self.queue.get_meta("employee_id")
        if raw_meta is None:
            return None

        try:
            emp_id = int(raw_meta)
            if emp_id <= 0:
                return None
        except (ValueError, TypeError):
            return None

        # Detect inconsistency between config.json and SQLite meta
        cfg_emp = getattr(self.config, "registered_employee_id", None)
        if cfg_emp is None:
            logger.warning("Employee binding inconsistency: missing in config while present in metadata. Failing closed.")
            return None
        try:
            if int(cfg_emp) != emp_id:
                logger.warning(
                    f"Employee binding inconsistency detected: config ({cfg_emp}) != metadata ({emp_id}). "
                    "Failing closed to prevent cross-account activity assignment."
                )
                return None
        except (ValueError, TypeError):
            return None

        return emp_id

    def register_and_bind(
        self,
        token: str,
        employee_id: int,
        employee_name: str,
        employee_email: Optional[str] = None,
    ) -> bool:
        """Atomically validates, protects, and persists registration credentials and employee binding.

        Fails closed: if token protection or employee validation fails, no partial or
        unbound authenticated state is left on the workstation.
        """
        if not token or not isinstance(token, str) or not token.strip():
            logger.warning("Registration rejected: empty or invalid token provided.")
            return False

        try:
            resolved_id = int(employee_id)
            if resolved_id <= 0:
                logger.warning(f"Registration rejected: non-positive employee_id '{employee_id}'.")
                return False
        except (ValueError, TypeError):
            logger.warning(f"Registration rejected: non-numeric employee_id '{employee_id}'.")
            return False

        # Prevent silently replacing an existing bound employee
        existing_id = self.get_authenticated_employee_id()
        if existing_id is not None and existing_id != resolved_id:
            logger.warning(
                f"Registration rejected: workstation is already bound to employee {existing_id}. "
                f"Cannot silently replace with employee {resolved_id} without explicit unpairing."
            )
            return False

        if not is_dpapi_available():
            logger.error("Registration rejected: Windows DPAPI is not available.")
            return False

        try:
            protected_token = protect_token(token.strip())

            # Transactionally store both credentials and binding together in SQLite
            if hasattr(self.queue, "set_auth_state_atomic"):
                self.queue.set_auth_state_atomic(
                    auth_token_protected=protected_token,
                    employee_id=resolved_id,
                    employee_name=employee_name or "Vaani Employee",
                    employee_email=employee_email,
                    auth_state="BOUND",
                )
            else:
                self.queue.set_meta("auth_token", protected_token)
                self.queue.set_meta("employee_id", resolved_id)
                self.queue.set_meta("employee_name", employee_name or "Vaani Employee")
                if employee_email:
                    self.queue.set_meta("employee_email", employee_email)
                self.queue.set_meta("auth_state", "BOUND")

            # Persist to config atomically
            self._cached_token = token.strip()
            self.config.auth_token = token.strip()
            self.config.registered_employee_id = resolved_id
            self.config.registered_employee_name = employee_name or "Vaani Employee"
            self.config.save()

            logger.info(f"Workstation paired and bound to employee {employee_name} (ID: {resolved_id})")
            return True
        except Exception as e:
            logger.error(f"Failed to securely persist registration credentials: {e}. Rolling back.")
            self.clear_credentials()
            return False

    def get_bound_employee(self) -> Dict[str, Any]:
        """Returns the currently bound employee info if authenticated, else unbound state."""
        auth_id = self.get_authenticated_employee_id()
        if not auth_id:
            return {
                "employee_id": None,
                "employee_name": None,
                "employee_email": None,
            }

        emp_name = self.queue.get_meta("employee_name") or self.config.registered_employee_name
        emp_email = self.queue.get_meta("employee_email")
        return {
            "employee_id": auth_id,
            "employee_name": emp_name,
            "employee_email": emp_email,
        }

    def clear_credentials(self) -> None:
        """Revokes local credentials and unbinds employee on logout/revocation.

        Preserves all local activity buckets in SQLite queue without deleting pending telemetry.
        """
        logger.warning("Clearing local device credentials and employee binding.")
        self._cached_token = None
        self.config.auth_token = None
        self.config.registered_employee_id = None
        self.config.registered_employee_name = None
        try:
            self.config.save()
        except Exception as e:
            logger.warning(f"Could not persist cleared config: {e}")

        if hasattr(self.queue, "set_auth_state_atomic"):
            self.queue.set_auth_state_atomic(
                auth_token_protected=None,
                employee_id=None,
                employee_name=None,
                employee_email=None,
                auth_state="CLEARED",
            )
        self.queue.clear_meta()

    def is_authenticated(self) -> bool:
        """Checks whether valid device credentials and authenticated employee binding are present."""
        return bool(self.get_authenticated_employee_id() and self.get_token())

