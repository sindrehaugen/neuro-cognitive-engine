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

variable "db_subnet_ids" {
  type = list(string)
}

variable "db_security_group_id" {
  type = string
}

variable "kms_key_arn" {
  type = string
}

variable "instance_class" {
  type = string
}

variable "environment" {
  type = string
}

locals {
  name = replace("trimcp-docdb-${var.deployment_name}", "_", "-")
}

resource "random_password" "master" {
  length  = 32
  special = false
}

resource "aws_docdb_subnet_group" "this" {
  name       = "${local.name}-subnets"
  subnet_ids = var.db_subnet_ids
}

resource "aws_docdb_cluster" "this" {
  cluster_identifier      = "${local.name}-cluster"
  engine                  = "docdb"
  master_username         = "trimcpdoc"
  master_password         = random_password.master.result
  db_subnet_group_name    = aws_docdb_subnet_group.this.name
  vpc_security_group_ids  = [var.db_security_group_id]
  storage_encrypted       = true
  kms_key_id              = var.kms_key_arn
  backup_retention_period = var.environment == "prod" ? 7 : 1
  skip_final_snapshot     = var.environment != "prod"
  deletion_protection     = var.environment == "prod"
}

resource "aws_docdb_cluster_instance" "this" {
  identifier                 = "${local.name}-0"
  cluster_identifier         = aws_docdb_cluster.this.id
  instance_class             = var.instance_class
  engine                     = aws_docdb_cluster.this.engine
  auto_minor_version_upgrade = true
}

resource "aws_secretsmanager_secret" "docdb" {
  name                    = "trimcp/${var.deployment_name}/documentdb"
  recovery_window_in_days = var.environment == "prod" ? 30 : 0
  kms_key_id              = var.kms_key_arn
}

resource "aws_secretsmanager_secret_version" "docdb" {
  secret_id = aws_secretsmanager_secret.docdb.id
  secret_string = jsonencode({
    username = "trimcpdoc"
    password = random_password.master.result
    host     = aws_docdb_cluster.this.endpoint
    port     = aws_docdb_cluster.this.port
    engine   = "documentdb"
    query    = "tls=true&replicaSet=rs0&readPreference=secondaryPreferred&retryWrites=false"
  })
}

output "endpoint" {
  value = aws_docdb_cluster.this.endpoint
}

output "secret_arn" {
  value = aws_secretsmanager_secret.docdb.arn
}
