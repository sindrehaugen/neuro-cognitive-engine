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

variable "db_subnet_ids" {
  type = list(string)
}

variable "db_security_group_id" {
  type = string
}

variable "kms_key_arn" {
  type = string
}

variable "db_name" {
  type = string
}

variable "engine_version" {
  type = string
}

variable "instance_class" {
  type = string
}

variable "allocated_storage" {
  type = number
}

variable "multi_az" {
  type = bool
}

variable "backup_retention" {
  type = number
}

variable "environment" {
  type    = string
  default = "dev"
}

locals {
  identifier = "trimcp-pg-${replace(var.deployment_name, "_", "-")}"
}

resource "random_password" "master" {
  length  = 32
  special = true
}

resource "aws_db_subnet_group" "this" {
  name       = "${local.identifier}-subnets"
  subnet_ids = var.db_subnet_ids
  tags       = { Name = "trimcp-${var.deployment_name}-db-subnets" }
}

resource "aws_db_parameter_group" "this" {
  name   = "${local.identifier}-pg16"
  family = "postgres16"
  tags   = { Name = "${local.identifier}-params" }
}

resource "aws_db_instance" "this" {
  identifier     = local.identifier
  engine         = "postgres"
  engine_version = var.engine_version

  instance_class        = var.instance_class
  allocated_storage     = var.allocated_storage
  max_allocated_storage = var.environment == "prod" ? var.allocated_storage * 2 : null
  storage_encrypted     = true
  kms_key_id            = var.kms_key_arn

  db_name  = var.db_name
  username = "trimcpadmin"
  password = random_password.master.result

  db_subnet_group_name   = aws_db_subnet_group.this.name
  vpc_security_group_ids = [var.db_security_group_id]
  parameter_group_name   = aws_db_parameter_group.this.name

  publicly_accessible   = false
  multi_az              = var.multi_az
  backup_retention_period = var.backup_retention

  skip_final_snapshot = var.environment != "prod"
  deletion_protection = var.environment == "prod"

  apply_immediately = var.environment != "prod"

  tags = { Name = local.identifier }
}

resource "aws_secretsmanager_secret" "postgres" {
  name                    = "trimcp/${var.deployment_name}/postgres"
  recovery_window_in_days = var.environment == "prod" ? 30 : 0
  kms_key_id              = var.kms_key_arn
}

resource "aws_secretsmanager_secret_version" "postgres" {
  secret_id = aws_secretsmanager_secret.postgres.id
  secret_string = jsonencode({
    username = "trimcpadmin"
    password = random_password.master.result
    host     = aws_db_instance.this.address
    port     = aws_db_instance.this.port
    dbname   = aws_db_instance.this.db_name
    engine   = "postgres"
    # Client builds DATABASE_URL after reading this secret — never commit.
  })
}

output "address" {
  value = aws_db_instance.this.address
}

output "port" {
  value = aws_db_instance.this.port
}

output "db_name" {
  value = aws_db_instance.this.db_name
}

output "secret_arn" {
  value = aws_secretsmanager_secret.postgres.arn
}
