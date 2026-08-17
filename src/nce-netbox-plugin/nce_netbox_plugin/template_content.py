from __future__ import annotations

from django.urls import reverse

try:
    from netbox.plugins import PluginTemplateContent
except ImportError:
    # TODO: Deprecate and remove this fallback block once NetBox <3.5 support is officially dropped
    from extras.plugins import PluginTemplateContent


class NceBaseCognitivePanel(PluginTemplateContent):
    """Abstract base class to DRY up rendering NCE cognitive panels."""

    def render_cognitive_panel(self, object_type: str):
        obj = self.context.get("object")
        if not obj:
            return ""
        name = getattr(obj, "name", None) or f"{object_type.capitalize()}-{obj.id}"
        return self.render(
            "nce_netbox_plugin/cognitive_panel.html",
            extra_context={
                "object_type": object_type,
                "object_name": name,
                "object_id": str(obj.id),
                "stats_api_url": reverse("plugins-api:nce_netbox_plugin-api:stats"),
            },
        )


class NceDeviceCognitivePanel(NceBaseCognitivePanel):
    """Hooks a Cognitive State Panel into NetBox Device detail pages."""

    model = "dcim.device"

    def left_page(self):
        return self.render_cognitive_panel("device")


class NceRackCognitivePanel(NceBaseCognitivePanel):
    """Hooks a Cognitive State Panel into NetBox Rack detail pages."""

    model = "dcim.rack"

    def right_page(self):
        return self.render_cognitive_panel("rack")


class NceSiteCognitivePanel(NceBaseCognitivePanel):
    """Hooks a Cognitive State Panel into NetBox Site detail pages."""

    model = "dcim.site"

    def full_width_page(self):
        return self.render_cognitive_panel("site")


template_extensions = [
    NceDeviceCognitivePanel,
    NceRackCognitivePanel,
    NceSiteCognitivePanel,
]
