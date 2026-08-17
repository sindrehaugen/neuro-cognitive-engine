"""
Shared module for unified Redis cache keys.
"""


def get_code_index_cache_key(namespace_id: str | None, user_id: str | None, filepath: str) -> str:
    """Build a deterministic Redis cache key for code indexing."""
    ns = str(namespace_id) if namespace_id else "global"
    user = user_id or "shared"
    safe_path = filepath.replace("\\", "/").rstrip("/")
    return f"code_index:{ns}:{user}:{safe_path}"
