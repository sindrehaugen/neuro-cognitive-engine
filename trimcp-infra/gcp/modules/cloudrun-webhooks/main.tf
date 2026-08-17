locals {
  name = replace("trimcp-${var.deployment_name}", "_", "-")
}

resource "google_cloud_run_v2_service" "webhooks" {
  name     = "${local.name}-webhooks"
  location = var.region
  ingress  = "INGRESS_TRAFFIC_ALL"

  template {
    scaling {
      min_instance_count = 0
      max_instance_count = 20
    }

    containers {
      image = var.image
    }
  }

  labels = {
    trimcp = "webhooks"
  }
}

resource "google_cloud_run_v2_service_iam_member" "public_invoke" {
  count    = var.allow_unauthenticated_invoke ? 1 : 0
  project  = var.project_id
  location = google_cloud_run_v2_service.webhooks.location
  name     = google_cloud_run_v2_service.webhooks.name
  role     = "roles/run.invoker"
  member   = "allUsers"
}

output "url" {
  description = "HTTPS URL — sole intentional public TriMCP ingress (pair with Cloud Armor LB in load-balancer module)"
  value       = google_cloud_run_v2_service.webhooks.uri
}
