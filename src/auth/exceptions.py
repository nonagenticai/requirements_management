"""Custom exceptions for JWT authentication.

This module defines custom exceptions for JWT token validation
and authentication errors in the requirements API.
"""


class InvalidTokenError(Exception):
    """Raised when a JWT token is invalid or malformed.

    This exception is raised when:
    - Token signature verification fails
    - Token structure is malformed
    - Token algorithm is not supported
    - Token validation fails for any other reason

    Attributes:
        message: Human-readable error message
        original_error: The original exception that caused this error (if any)
    """

    def __init__(self, message: str, original_error: Exception | None = None) -> None:
        """Initialize InvalidTokenError.

        Args:
            message: Human-readable error message describing the validation failure
            original_error: The original exception that caused this error (optional)
        """
        self.message = message
        self.original_error = original_error
        super().__init__(self.message)

    def __str__(self) -> str:
        """Return string representation of the error."""
        if self.original_error:
            return f"{self.message} (caused by: {str(self.original_error)})"
        return self.message


class ExpiredTokenError(Exception):
    """Raised when a JWT token has expired.

    This exception is raised when the token's expiration time (exp claim)
    is in the past.

    Attributes:
        message: Human-readable error message
        expired_at: The timestamp when the token expired (if available)
    """

    def __init__(self, message: str, expired_at: int | None = None) -> None:
        """Initialize ExpiredTokenError.

        Args:
            message: Human-readable error message
            expired_at: Unix timestamp when the token expired (optional)
        """
        self.message = message
        self.expired_at = expired_at
        super().__init__(self.message)

    def __str__(self) -> str:
        """Return string representation of the error."""
        if self.expired_at:
            return f"{self.message} (expired at: {self.expired_at})"
        return self.message


class MissingCreatorIdError(Exception):
    """Raised when the creator_id claim is missing from a JWT token.

    This exception is raised when a valid JWT token does not contain
    the required 'creator_id' claim needed for creator identification.

    Attributes:
        message: Human-readable error message
        available_claims: List of claims that were present in the token
    """

    def __init__(self, message: str, available_claims: list[str] | None = None) -> None:
        """Initialize MissingCreatorIdError.

        Args:
            message: Human-readable error message
            available_claims: List of claim names that were present in the token (optional)
        """
        self.message = message
        self.available_claims = available_claims or []
        super().__init__(self.message)

    def __str__(self) -> str:
        """Return string representation of the error."""
        if self.available_claims:
            return f"{self.message} (available claims: {', '.join(self.available_claims)})"
        return self.message
