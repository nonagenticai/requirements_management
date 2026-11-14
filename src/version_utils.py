"""
Version comparison utilities for principles and decisions.

Provides utilities for:
- Comparing semantic version strings
- Detecting breaking changes
- Validating version formats
"""

import logging

from packaging import version

logger = logging.getLogger(__name__)


def compare_versions(v1: str, v2: str) -> int:
    """Compare two semantic version strings.

    Args:
        v1: First version string (e.g. "1.0.0")
        v2: Second version string (e.g. "2.0.0")

    Returns:
        -1 if v1 < v2
         0 if v1 == v2
         1 if v1 > v2

    Raises:
        ValueError: If version strings are invalid

    Examples:
        >>> compare_versions("1.0.0", "2.0.0")
        -1
        >>> compare_versions("2.0.0", "1.0.0")
        1
        >>> compare_versions("1.0.0", "1.0.0")
        0
    """
    try:
        parsed_v1 = version.parse(v1)
        parsed_v2 = version.parse(v2)

        if parsed_v1 < parsed_v2:
            return -1
        elif parsed_v1 > parsed_v2:
            return 1
        else:
            return 0
    except ValueError as e:
        raise ValueError(f"Invalid version string: {e}") from e


def is_breaking_change(old: str, new: str) -> bool:
    """Check if transitioning from old to new version is a breaking change.

    A breaking change occurs when the major version number increases.

    Args:
        old: Old version string (e.g. "1.5.3")
        new: New version string (e.g. "2.0.0")

    Returns:
        True if major version changed, False otherwise

    Examples:
        >>> is_breaking_change("1.5.3", "2.0.0")
        True
        >>> is_breaking_change("1.5.3", "1.6.0")
        False
        >>> is_breaking_change("2.0.0", "1.5.0")
        False
    """
    try:
        parsed_old = version.parse(old)
        parsed_new = version.parse(new)

        # Extract major version
        old_major = _get_major_version(parsed_old)
        new_major = _get_major_version(parsed_new)

        # Breaking change only if new major version is greater
        return new_major > old_major
    except (version.InvalidVersion, TypeError) as exc:
        logger.warning("is_breaking_change failed for %r -> %r: %s", old, new, exc)
        return False


def _get_major_version(v: version.Version) -> int:
    """Extract major version number from a Version object.

    Args:
        v: Parsed version object

    Returns:
        Major version number as integer
    """
    # packaging.version.Version has a release tuple
    if hasattr(v, "release") and v.release:
        return v.release[0]
    return 0


def validate_version_format(version_string: str) -> bool:
    """Validate that a version string follows semantic versioning format.

    Args:
        version_string: Version string to validate

    Returns:
        True if valid, False otherwise

    Examples:
        >>> validate_version_format("1.0.0")
        True
        >>> validate_version_format("1.0")
        True
        >>> validate_version_format("invalid")
        False
    """
    try:
        version.parse(version_string)
        return True
    except (version.InvalidVersion, TypeError) as exc:
        logger.debug("Invalid version format %r: %s", version_string, exc)
        return False


def parse_version_parts(version_string: str) -> tuple[int, int, int]:
    """Parse a semantic version string into major, minor, patch components.

    Args:
        version_string: Version string to parse (e.g. "1.2.3")

    Returns:
        Tuple of (major, minor, patch) integers

    Raises:
        ValueError: If version string is invalid or doesn't have 3 parts

    Examples:
        >>> parse_version_parts("1.2.3")
        (1, 2, 3)
        >>> parse_version_parts("2.0.0")
        (2, 0, 0)
    """
    try:
        parsed = version.parse(version_string)
        if hasattr(parsed, "release") and len(parsed.release) >= 3:
            return (parsed.release[0], parsed.release[1], parsed.release[2])
        elif hasattr(parsed, "release") and len(parsed.release) == 2:
            return (parsed.release[0], parsed.release[1], 0)
        else:
            raise ValueError("Version must have at least major.minor format")
    except ValueError as e:
        raise ValueError(f"Invalid version string: {e}") from e


def increment_version(current: str, bump_type: str = "patch") -> str:
    """Increment a version string by bump type.

    Args:
        current: Current version string (e.g. "1.2.3")
        bump_type: Type of version bump - "major", "minor", or "patch"

    Returns:
        New incremented version string

    Raises:
        ValueError: If version string is invalid or bump_type is unknown

    Examples:
        >>> increment_version("1.2.3", "patch")
        '1.2.4'
        >>> increment_version("1.2.3", "minor")
        '1.3.0'
        >>> increment_version("1.2.3", "major")
        '2.0.0'
    """
    try:
        major, minor, patch = parse_version_parts(current)

        if bump_type == "major":
            return f"{major + 1}.0.0"
        elif bump_type == "minor":
            return f"{major}.{minor + 1}.0"
        elif bump_type == "patch":
            return f"{major}.{minor}.{patch + 1}"
        else:
            raise ValueError(
                f"Unknown bump_type: {bump_type}. Must be 'major', 'minor', or 'patch'"
            )
    except ValueError as e:
        raise ValueError(f"Failed to increment version: {e}") from e
