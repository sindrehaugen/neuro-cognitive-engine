# Non-secret references only (Appendix I.7). Resolve secret payloads with gcloud/Secret Manager.

output "deployment_name" {
  value = var.deployment_name
}

output "region" {
  value = var.region
}

output "postgres_private_ip" {
  value = module.cloudsql.private_ip
}

output "postgres_database_name" {
  value = module.cloudsql.database_name
}

output "postgres_secret_id" {
  value = module.cloudsql.admin_secret_id
}

output "redis_host" {
  value = module.memorystore.host
}

output "redis_secret_id" {
  value = module.memorystore.auth_secret_id
}

output "mongo_connection_secret_id" {
  value = module.mongo.connection_secret_id
}

output "gcs_bucket_name" {
  value = module.gcs.bucket_name
}

output "webhook_public_url" {
  description = "Only service with allUsers invoker in this stack (Appendix I.6)"
  value       = module.cloudrun_webhooks.url
}

output "worker_cloud_run_uri" {
  description = "Internal-only worker URL"
  value       = module.cloudrun_worker.uri
}

output "monitoring_dashboard_id" {
  value = module.monitoring.dashboard_id
}
