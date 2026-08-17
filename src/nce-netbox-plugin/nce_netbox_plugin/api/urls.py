from __future__ import annotations

from django.urls import path

from nce_netbox_plugin.api.views import NceCognitiveStatsView

urlpatterns = [
    path("stats/", NceCognitiveStatsView.as_view(), name="stats"),
]
