"""Application-level errors mapped to HTTP responses in the API layer."""

from __future__ import annotations

from typing import Any


class AppError(Exception):
    """Base class for errors we deliberately surface to the caller."""

    status_code: int = 400
    code: str = "app_error"

    def __init__(self, message: str, *, details: Any = None, code: str | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.details = details
        if code:
            self.code = code


class NotFoundError(AppError):
    status_code = 404
    code = "not_found"


class ConflictError(AppError):
    status_code = 409
    code = "conflict"


class ValidationError(AppError):
    status_code = 422
    code = "validation_failed"


class BudgetExceededError(AppError):
    """Raised whenever proceeding could spend more than the user authorised."""

    status_code = 409
    code = "budget_exceeded"


class ModeNotAllowedError(AppError):
    """The requested operation is disabled in the current APP_MODE."""

    status_code = 409
    code = "mode_not_allowed"


class ConfirmationRequiredError(AppError):
    status_code = 400
    code = "confirmation_required"


class WebhookVerificationError(AppError):
    status_code = 401
    code = "invalid_signature"
