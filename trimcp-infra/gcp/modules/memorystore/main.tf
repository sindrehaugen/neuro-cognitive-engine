locals {
  name = replace("trimcp-${var.deployment_name}", "_", "-")
}

resource "google_redis_instance" "cache" {
  name           = "${local.name}-redis"
  tier           = "BASIC"
  memory_size_gb = var.memory_size_gb
  region         = var.region
  redis_version  = "REDIS_7_0"

  authorized_network = var.network_id
  connect_mode       = "DIRECT_PEERING"

  auth_enabled = true

  labels = var.labels
}

resource "google_secret_manager_secret" "redis" {
  secret_id = "${local.name}-redis-auth"

  replication {
    auto {}
  }
}

resource "google_secret_manager_secret_iam_member" "worker_redis_accessor" {
  secret_id = google_secret_manager_secret.redis.id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${var.worker_service_account_email}"
}

resource "google_secret_manager_secret_version" "redis" {
  secret = google_secret_manager_secret.redis.id
  secret_data = jsonencode({
    host        = google_redis_instance.cache.host
    port        = google_redis_instance.cache.port
    auth_string = google_redis_instance.cache.auth_string
  })
}

output "host" {
  value = google_redis_instance.cache.host
}

output "port" {
  value = google_redis_instance.cache.port
}

output "auth_secret_id" {
  value = google_secret_manager_secret.redis.secret_id
}
