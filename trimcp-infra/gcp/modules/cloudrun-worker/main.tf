locals {
  name = replace("trimcp-${var.deployment_name}", "_", "-")
}

resource "google_cloud_run_v2_service" "worker" {
  name     = "${local.name}-rq-worker"
  location = var.region
  ingress  = "INGRESS_TRAFFIC_INTERNAL_ONLY"

  template {
    service_account = var.service_account_email

    scaling {
      min_instance_count = 0
      max_instance_count = 4
    }

    max_instance_request_concurrency = 10

    vpc_access {
      connector = var.vpc_connector_id
      egress    = "ALL_TRAFFIC"
    }

    containers {
      image = var.image
      args  = [] # TriMCP worker entrypoint replaces this

      env {
        name  = "TRIMCP_POSTGRES_SECRET"
        value = var.postgres_secret_id
      }
      env {
        name  = "TRIMCP_REDIS_SECRET"
        value = var.redis_secret_id
      }
      env {
        name  = "TRIMCP_MONGO_SECRET"
        value = var.mongo_secret_id
      }
      env {
        name  = "TRIMCP_GCS_BUCKET"
        value = var.gcs_bucket_name
      }
    }
  }

  labels = {
    trimcp = "worker"
  }
}

output "service_name" {
  value = google_cloud_run_v2_service.worker.name
}

output "uri" {
  description = "Internal-only URI (not public; I.6)"
  value       = google_cloud_run_v2_service.worker.uri
}
