"""
Input validation and sanitization utilities.

Provides functions to sanitize and validate input data before processing.
"""

import math
import unicodedata
from typing import Any


def sanitize_unicode_string(text: str) -> str:
    """Sanitize Unicode strings to prevent encoding issues.

    Args:
        text: Input string that may contain invalid Unicode

    Returns:
        Sanitized string with invalid Unicode characters removed
    """
    if not isinstance(text, str):
        return str(text)

    # Normalize Unicode to NFC form
    try:
        normalized = unicodedata.normalize("NFC", text)
        # Remove any remaining problematic characters
        sanitized = "".join(
            char for char in normalized if unicodedata.category(char)[0] != "C" or char in "\t\n\r"
        )
        return sanitized
    except (UnicodeError, ValueError):  # fmt: skip
        # If normalization fails, encode/decode to remove invalid sequences
        return text.encode("utf-8", errors="ignore").decode("utf-8", errors="ignore")


def sanitize_dict(data: dict[str, Any]) -> dict[str, Any]:
    """Recursively sanitize dictionary values.

    Args:
        data: Dictionary that may contain invalid Unicode strings

    Returns:
        Dictionary with sanitized string values
    """
    if not isinstance(data, dict):
        return data

    sanitized: dict[str, Any] = {}
    for key, value in data.items():
        # Sanitize key
        clean_key = sanitize_unicode_string(str(key))

        # Sanitize value based on type
        if isinstance(value, str):
            sanitized[clean_key] = sanitize_unicode_string(value)
        elif isinstance(value, dict):
            sanitized[clean_key] = sanitize_dict(value)
        elif isinstance(value, list):
            sanitized[clean_key] = sanitize_list(value)
        else:
            sanitized[clean_key] = value

    return sanitized


def sanitize_list(data: list[Any]) -> list[Any]:
    """Recursively sanitize list values.

    Args:
        data: List that may contain invalid Unicode strings

    Returns:
        List with sanitized string values
    """
    if not isinstance(data, list):
        return data

    sanitized: list[Any] = []
    for item in data:
        if isinstance(item, str):
            sanitized.append(sanitize_unicode_string(item))
        elif isinstance(item, dict):
            sanitized.append(sanitize_dict(item))
        elif isinstance(item, list):
            sanitized.append(sanitize_list(item))
        else:
            sanitized.append(item)

    return sanitized


def validate_float_bounds(
    value: float,
    name: str,
    min_val: float | None = None,
    max_val: float | None = None,
) -> float:
    """Validate float values are within reasonable bounds.

    Args:
        value: Float value to validate
        name: Parameter name for error messages
        min_val: Minimum allowed value
        max_val: Maximum allowed value

    Returns:
        Validated float value

    Raises:
        ValueError: If value is outside bounds or invalid
    """
    if not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be a number")

    # Check for NaN and Infinity
    if math.isnan(value):
        raise ValueError(f"{name} cannot be NaN")

    if math.isinf(value):
        raise ValueError(f"{name} cannot be infinite")

    # Check for extreme values that could cause issues
    if abs(value) < 1e-10 and value != 0:
        raise ValueError(f"{name} value {value} is too small (< 1e-10)")

    if abs(value) > 1e10:
        raise ValueError(f"{name} value {value} is too large (> 1e10)")

    # Check custom bounds
    if min_val is not None and value < min_val:
        raise ValueError(f"{name} must be >= {min_val}")

    if max_val is not None and value > max_val:
        raise ValueError(f"{name} must be <= {max_val}")

    return float(value)
