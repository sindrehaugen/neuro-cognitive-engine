terraform {
  required_providers {
    random = {
      source  = "hashicorp/random"
      version = "~> 3.6"
    }
  }
}

variable "deployment_name" {
  type = string
}

variable "vpc_id" {
  type = string
}

variable "subnet_ids" {
  type = list(string)
}

variable "db_security_group_id" {
  type = string
}

variable "kms_key_arn" {
  type = string # used for secrets; ElastiCache has built-in encryption
}

variable "node_type" {
  type = string
}

variable "environment" {
  type = string
}

locals {
  id = replace("trimcp-${var.deployment_name}-redis", "_", "-")
}

resource "random_password" "auth" {
  length  = 32
  special = false
}

resource "aws_elasticache_subnet_group" "this" {
  name       = "${local.id}-subnets"
  subnet_ids = var.subnet_ids
}

resource "aws_elasticache_replication_group" "this" {
  replication_group_id       = local.id
  description                = "TriMCP Redis (${var.deployment_name})"
  node_type                  = var.node_type
  port                       = 6379
  parameter_group_name       = "default.redis7"
  subnet_group_name          = aws_elasticache_subnet_group.this.name
  security_group_ids         = [var.db_security_group_id]
  at_rest_encryption_enabled = true
  transit_encryption_enabled = true
  auth_token                 = random_password.auth.result

  automatic_failover_enabled = var.environment == "prod"
  num_cache_clusters         = var.environment == "prod" ? 2 : 1

  apply_immediately = var.environment != "prod"

  tags = { Name = local.id }
}

resource "aws_secretsmanager_secret" "redis" {
  name                    = "trimcp/${var.deployment_name}/redis"
  recovery_window_in_days = var.environment == "prod" ? 30 : 0
  kms_key_id              = var.kms_key_arn
}

resource "aws_secretsmanager_secret_version" "redis" {
  secret_id = aws_secretsmanager_secret.redis.id
  secret_string = jsonencode({
    host     = aws_elasticache_replication_group.this.primary_endpoint_address
    port     = aws_elasticache_replication_group.this.port
    auth_token = random_password.auth.result
    tls      = true
  })
}

output "primary_endpoint_address" {
  value = aws_elasticache_replication_group.this.primary_endpoint_address
}

output "port" {
  value = aws_elasticache_replication_group.this.port
}

output "auth_secret_arn" {
  value = aws_secretsmanager_secret.redis.arn
}
