# --- Appendix I.2 common variables ---
variable "deployment_name" {
  type        = string
  description = "Short globally unique deployment id (e.g. acme-prod)"
}

variable "region" {
  type        = string
  description = "AWS region (e.g. eu-west-1)"
}

variable "environment" {
  type        = string
  description = "dev | staging | prod"
}

variable "vpc_cidr" {
  type        = string
  default     = "10.10.0.0/16"
  description = "VPC CIDR (private RFC1918)"
}

variable "vpn_subnet_cidr" {
  type        = string
  default     = "10.100.0.0/24"
  description = "Logical CIDR for VPN clients (used in security documentation / future peering)"
}

variable "webhook_dns_name" {
  type        = string
  default     = ""
  description = "Public FQDN for webhooks (configure in DNS to point at API Gateway / ALB)"
}

variable "postgres_database_name" {
  type    = string
  default = "memory_meta"
}

variable "postgres_engine_version" {
  type    = string
  default = "16.4"
}

variable "db_size_postgres" {
  type    = string
  default = "small"
}

variable "db_size_mongo" {
  type    = string
  default = "small"
}

variable "db_size_redis" {
  type    = string
  default = "small"
}

variable "worker_container_image" {
  type        = string
  description = "ECR image URI for RQ worker (e.g. 123.dkr.ecr.region.amazonaws.com/trimcp-worker:latest)"
  default     = "public.ecr.aws/docker/library/busybox:latest"
}

variable "worker_cpu" {
  type    = number
  default = 1024
}

variable "worker_memory" {
  type    = number
  default = 4096
}

variable "worker_desired_count" {
  type    = number
  default = 1
}

variable "webhook_lambda_placeholder_zip" {
  type        = string
  default     = ""
  description = "Optional path to zip for webhook Lambda; if empty, uses inline archive in module"
}

variable "tags" {
  type    = map(string)
  default = {}
}

locals {
  postgres_instance_class = var.db_size_postgres == "prod" || var.db_size_postgres == "large" ? "db.m7g.large" : "db.t4g.medium"
  postgres_allocated_storage = var.environment == "prod" ? 100 : 50

  documentdb_instance_class = var.db_size_mongo == "prod" || var.db_size_mongo == "large" ? "db.r6g.large" : "db.t4g.medium"

  redis_node_type = var.db_size_redis == "prod" || var.db_size_redis == "large" ? "cache.m6g.large" : "cache.t4g.small"
}
