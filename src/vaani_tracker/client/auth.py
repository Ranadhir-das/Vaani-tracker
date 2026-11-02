"""Device authentication and employee binding manager."""

import logging
from typing import Optional, Dict, Any

from ..config import TrackerConfig
from ..models import DeviceInfo
from ..storage.queue import ActivityQueue

logger = logging.getLogger(__name__)


class DeviceAuthManager:
    """Manages secure device registration tokens and employee identity binding."""

    def __init__(self, config: TrackerConfig, queue: ActivityQueue):
        self.config = config
        self.queue = queue

    def get_token(self) -> Optional[str]:
        """Returns the active device authentication token."""
        token = self.queue.get_meta("auth_token")
        if not token:
            token = self.config.auth_token
        return token

    def set_token(self, token: str) -> None:
        """Saves the device authentication token."""
        self.config.auth_token = token
        self.config.save()
        self.queue.set_meta("auth_token", token)

    def bind_employee(self, employee_id: int, employee_name: str, employee_email: Optional[str] = None) -> None:
        """Binds the local device to the authenticated employee."""
        self.config.registered_employee_id = employee_id
        self.config.registered_employee_name = employee_name
        self.config.save()

        self.queue.set_meta("employee_id", employee_id)
        self.queue.set_meta("employee_name", employee_name)
        if employee_email:
            self.queue.set_meta("employee_email", employee_email)
        logger.info(f"Device bound to employee: {employee_name} (ID: {employee_id})")

    def get_bound_employee(self) -> Dict[str, Any]:
        """Returns the currently bound employee info."""
        emp_id = self.queue.get_meta("employee_id", self.config.registered_employee_id)
        emp_name = self.queue.get_meta("employee_name", self.config.registered_employee_name)
        emp_email = self.queue.get_meta("employee_email")
        return {
            "employee_id": emp_id,
            "employee_name": emp_name,
            "employee_email": emp_email,
        }

    def clear_credentials(self) -> None:
        """Revokes local credentials and unbinds employee on logout/revocation."""
        logger.warning("Clearing local device credentials and employee binding.")
        self.config.auth_token = None
        self.config.registered_employee_id = None
        self.config.registered_employee_name = None
        self.config.save()

        self.queue.clear_meta()

    def is_authenticated(self) -> bool:
        """Checks whether valid device credentials are present."""
        token = self.get_token()
        emp = self.get_bound_employee()
        return bool(token and emp.get("employee_id"))

