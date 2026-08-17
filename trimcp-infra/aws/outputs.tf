# Non-secret outputs only (Appendix I.7). Use secret ARNs with CLI to resolve credentials.

output "deployment_name" {
  value = var.deployment_name
}

output "region" {
  value = var.region
}

output "vpc_id" {
  value = module.network.vpc_id
}

output "postgres_endpoint_address" {
  value = module.rds_postgres.address
}

output "postgres_port" {
  value = module.rds_postgres.port
}

output "postgres_database_name" {
  value = module.rds_postgres.db_name
}

output "postgres_secret_arn" {
  value = module.rds_postgres.secret_arn
}

output "documentdb_endpoint" {
  description = "DocumentDB cluster endpoint (private DNS)"
  value       = module.documentdb.endpoint
}

output "mongo_secret_arn" {
  description = "Secrets Manager ARN holding DocumentDB URI components"
  value       = module.documentdb.secret_arn
}

output "redis_primary_endpoint" {
  value = module.elasticache.primary_endpoint_address
}

output "redis_port" {
  value = module.elasticache.port
}

output "redis_auth_secret_arn" {
  value = module.elasticache.auth_secret_arn
}

output "s3_bucket_id" {
  value = module.s3.bucket_id
}

output "blob_endpoint" {
  description = "S3 regional endpoint for SDKs"
  value       = "s3.${var.region}.amazonaws.com"
}

output "blob_bucket_name" {
  value = module.s3.bucket_id
}

output "webhook_invoke_url" {
  description = "Public HTTPS URL for bridge webhooks (append /webhooks/{provider})"
  value       = module.webhook_api.invoke_url
}

output "webhook_public_base_url" {
  value = module.webhook_api.invoke_url
}

output "kms_key_id" {
  value = module.secrets.kms_key_id
}
