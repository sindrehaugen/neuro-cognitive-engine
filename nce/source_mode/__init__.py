"""
nce.source_mode — C5 source-mode resolver.

Exposes ``resolve``, ``read_through``, and ``write_route`` as the public
interface for the source-mode dispatch layer.  Consumers should import from
this package rather than directly from ``nce.source_mode.resolver``.
"""

from nce.source_mode.resolver import (
    SourceMode,
    read_through,
    resolve,
    write_route,
)

__all__ = ["SourceMode", "read_through", "resolve", "write_route"]
