"""
nce/vertical_modules/system_design/__init__.py
==============================================
System Design Engine vertical module (Module 3).

Exports public core functions for topology authoring, design proposals,
geometry, Lucidchart publishing, SOW generation, and quote-design bidirectional sync.
"""

from __future__ import annotations

from nce.vertical_modules.system_design.devices import (
    do_author_device_topology,
)
from nce.vertical_modules.system_design.enrichment import (
    do_enrich_design_lines,
)
from nce.vertical_modules.system_design.from_quote import (
    do_design_from_quote,
)
from nce.vertical_modules.system_design.geometry import (
    do_author_functional_location_geometry,
    do_author_geometry,
)
from nce.vertical_modules.system_design.graph import (
    do_author_functional_location,
)
from nce.vertical_modules.system_design.lucid import (
    do_publish_design_docs,
)
from nce.vertical_modules.system_design.propose import (
    do_propose_design,
)
from nce.vertical_modules.system_design.read import (
    do_get_topology,
)
from nce.vertical_modules.system_design.retire import (
    do_retire_planned,
)
from nce.vertical_modules.system_design.sow import (
    do_generate_sow,
)
from nce.vertical_modules.system_design.to_quote import (
    do_design_to_quote,
)
from nce.vertical_modules.system_design.validate import (
    do_validate_design,
)

__all__ = [
    "do_author_device_topology",
    "do_author_functional_location",
    "do_author_functional_location_geometry",
    "do_author_geometry",
    "do_design_from_quote",
    "do_design_to_quote",
    "do_enrich_design_lines",
    "do_generate_sow",
    "do_get_topology",
    "do_propose_design",
    "do_publish_design_docs",
    "do_retire_planned",
    "do_validate_design",
]
