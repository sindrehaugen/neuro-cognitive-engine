# Findings: OQ-3 The 135-vs-78 tool gap

## Investigation
An analysis of the NCE repository at baseline `7e97efe` reveals the following about the MCP tool discrepancy:

1. **`TOOL_REGISTRY` Context (`nce/tool_registry.py`)**: 
   The `TOOL_REGISTRY` explicitly registers exactly 135 tools. The 57 "missing" tools belong to various vertical modules:
   - Dynamics 365 (`d365_query_case`, etc.)
   - Product (`product_search`, `product_price`, etc.)
   - Procurement (`procurement_calculate_tco`, etc.)
   - System Design (`system_design_ping`, etc.)
   - Project (`project_can_enter_phase`, etc.)
   - Sales (`sales_ping`, etc.)
   - Vendors (`vendors_get_vendor`, etc.)
   - Agreements (`agreements_lookup_terms`, etc.)
   - Economy (`economy_match_invoice`, etc.)

2. **Intentional MCP Handlers**:
   These 57 tools are *not* strictly REST endpoints. In `TOOL_REGISTRY`, they are wired to explicit MCP handlers (e.g., `nce.vertical_modules.dynamics365.mcp_handlers`) and they specify MCP dispatch metadata flags (`cacheable`, `mutation`, `admin_only`). This indicates they are definitively designed to be executed via the MCP dispatch loop (`mcp_stdio_dispatch.py`), which uses `TOOL_REGISTRY.get(name)` to route calls.

3. **The Schema Gap (`nce/mcp_stdio_tools.py`)**:
   While the backend dispatch logic knows about these 57 tools, their schema definitions (the `mcp.types.Tool` objects defining `inputSchema` and descriptions) are missing from the `TOOLS` list in `nce/mcp_stdio_tools.py`.
   Because `server.py::list_tools()` returns this static `TOOLS` list verbatim, the MCP clients only see the 78 tools defined there. 

## Conclusion
The gap does not appear to be intentional by design (e.g., restricted to REST only), because the tools are actively wired with MCP-specific handlers and dispatch flags. Instead, this is an architectural desync:
- The backend execution layer (`TOOL_REGISTRY`) was updated to include vertical module tools.
- The presentation/schema layer (`TOOLS` list in `mcp_stdio_tools.py`) was not updated with the corresponding `mcp.types.Tool` schemas for these vertical modules.
- Thus, the vertical tools are dispatchable (if a client somehow knew the exact schema and tool name to call) but entirely undiscoverable via the standard MCP `tools/list` handshake.

**Update (2026-09-01):** PR #159 converts this architectural gap into an enumerated work-list owned by ML waves 230c–230e. The test `tests/unit/test_mcp_tool_surface_ratchet.py` now explicitly pins the 135 registered / 92 defined / 43 missing counts, replacing this open question with a tracked burndown.
