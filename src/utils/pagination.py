"""Query parameter normalization utilities."""

from validation import sanitize_unicode_string


def normalize_search_query(q: str | None) -> str | None:
    """Normalize and validate search query parameter.

    Args:
        q: Raw query string from request

    Returns:
        Sanitized query string or None if invalid/empty
    """
    if q is None or q == "null" or not q.strip():
        return None
    result = sanitize_unicode_string(q.strip())
    return str(result) if result is not None else None


def normalize_k_param(k: int, default: int = 10, max_val: int = 100) -> int:
    """Normalize k (limit) parameter for search results.

    Args:
        k: Raw k value
        default: Default value if invalid
        max_val: Maximum allowed value

    Returns:
        Normalized integer between 1 and max_val
    """
    try:
        k = int(k)
        if k < 1:
            return 1
        if k > max_val:
            return max_val
        return k
    except (ValueError, TypeError):  # fmt: skip
        return default


def normalize_min_similarity(min_sim: float, default: float = 0.1) -> float:
    """Normalize min_similarity parameter.

    Args:
        min_sim: Raw similarity threshold
        default: Default value if invalid

    Returns:
        Float between 0.0 and 1.0
    """
    try:
        min_sim = float(min_sim)
        if min_sim < 0:
            return 0.0
        if min_sim > 1:
            return 1.0
        return min_sim
    except ValueError, TypeError, OverflowError:
        return default
