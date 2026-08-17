locals {
  title = "TriMCP ${var.deployment_name}"
}

resource "google_monitoring_dashboard" "trimcp" {
  project        = var.project_id
  dashboard_json = jsonencode({
    displayName = local.title
    mosaicLayout = {
      columns = 12
      tiles   = []
    }
  })
}

output "dashboard_id" {
  value = google_monitoring_dashboard.trimcp.id
}
