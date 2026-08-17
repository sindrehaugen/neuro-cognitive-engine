from __future__ import annotations

try:
    from netbox.plugins import PluginConfig
except ImportError:
    # Fallback support for legacy environments
    from extras.plugins import PluginConfig


class NceCognitiveDashboardConfig(PluginConfig):
    """
    NetBox plugin configuration class for NCE Cognitive Dashboard (BATCH-P3-NB-006).
    Registers layout extensions and API endpoints within the NetBox plugin framework.
    """

    name = "nce_netbox_plugin"
    verbose_name = "NCE Cognitive Dashboard"
    description = "Integrates NCE operator stress, incidents, and fault maps into NetBox"
    version = "0.1.0"
    author = "NCE Architecture Team"
    author_email = "admin@nce.internal"
    base_url = "nce-cognitive-dashboard"
    required_settings = []
    default_settings = {
        "nce_admin_api_url": "http://localhost:8000",
    }


config = NceCognitiveDashboardConfig
