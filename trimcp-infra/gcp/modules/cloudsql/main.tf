locals {
  name     = replace("trimcp-${var.deployment_name}", "_", "-")
  db_name  = "memory_meta"
}

resource "random_password" "postgres" {
  length  = 24
  special = false
}

resource "google_sql_database_instance" "postgres" {
  name             = "${local.name}-pg"
  database_version = "POSTGRES_16"
  region           = var.region

  settings {
    tier              = var.tier
    disk_type         = "PD_SSD"
    disk_size         = var.disk_size_gb
    availability_type = var.environment == "prod" ? "REGIONAL" : "ZONAL"

    ip_configuration {
      ipv4_enabled                                  = false
      private_network                               = var.network_id
      enable_private_path_for_google_cloud_services = true
    }

    database_flags {
      name  = "cloudsql.enable_pgvector"
      value = "on"
    }

    user_labels = var.labels
  }

  deletion_protection = var.environment == "prod"
}

resource "google_sql_database" "app" {
  name     = local.db_name
  instance = google_sql_database_instance.postgres.name
}

resource "google_sql_user" "admin" {
  name     = "trimcp_admin"
  instance = google_sql_database_instance.postgres.name
  password = random_password.postgres.result
}

resource "google_secret_manager_secret" "db_admin" {
  secret_id = "${local.name}-pg-admin"

  replication {
    auto {}
  }
}

resource "google_secret_manager_secret_iam_member" "worker_db_accessor" {
  secret_id = google_secret_manager_secret.db_admin.id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${var.worker_service_account_email}"
}

resource "google_secret_manager_secret_version" "db_admin" {
  secret      = google_secret_manager_secret.db_admin.id
  secret_data = jsonencode({
    host         = google_sql_database_instance.postgres.private_ip_address
    port         = 5432
    database     = local.db_name
    username     = google_sql_user.admin.name
    password     = random_password.postgres.result
    pgvector     = true
    sslmode      = "require"
    note         = "CREATE EXTENSION IF NOT EXISTS vector; after first connect if not already present"
  })
}

output "private_ip" {
  value     = google_sql_database_instance.postgres.private_ip_address
  sensitive = false
}

output "database_name" {
  value = local.db_name
}

output "admin_secret_id" {
  description = "Secret Manager secret id (not the payload)"
  value       = google_secret_manager_secret.db_admin.secret_id
}

output "admin_secret_resource_name" {
  value = google_secret_manager_secret.db_admin.id
}
