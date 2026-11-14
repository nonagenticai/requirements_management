"""Version reference validation utilities."""


def validate_version_refs(
    supersedes: str | None,
    superseded_by: str | None,
    slug: str,
) -> None:
    """Validate version reference fields to prevent circular references.

    Raises:
        ValueError: if both supersedes and superseded_by are set, or if either references self.
    """
    if supersedes and superseded_by:
        raise ValueError("Cannot set both supersedes and superseded_by")
    if supersedes == slug or superseded_by == slug:
        raise ValueError("Cannot reference self in version chain")
