terraform {
  required_version = ">= 1.6.0"
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
    random = {
      source  = "hashicorp/random"
      version = "~> 3.6"
    }
    archive = {
      source  = "hashicorp/archive"
      version = "~> 2.4"
    }
  }
}

provider "aws" {
  region = var.region
  default_tags {
    tags = merge(
      var.tags,
      {
        "trimcp:deployment" = var.deployment_name
        "trimcp:environment" = var.environment
      }
    )
  }
}

data "aws_caller_identity" "current" {}
data "aws_availability_zones" "available" { state = "available" }

locals {
  azs = slice(data.aws_availability_zones.available.names, 0, 2)
}

module "network" {
  source          = "./modules/network"
  deployment_name = var.deployment_name
  region          = var.region
  vpc_cidr        = var.vpc_cidr
  vpn_subnet_cidr = var.vpn_subnet_cidr
  availability_zones = local.azs
}

module "secrets" {
  source          = "./modules/secrets"
  deployment_name = var.deployment_name
  environment     = var.environment
}

module "s3" {
  source          = "./modules/s3"
  deployment_name = var.deployment_name
  aws_region      = var.region
  kms_key_arn     = module.secrets.kms_key_arn
}

module "rds_postgres" {
  source                = "./modules/rds-postgres"
  deployment_name       = var.deployment_name
  environment           = var.environment
  db_subnet_ids         = module.network.private_db_subnet_ids
  db_security_group_id  = module.network.data_security_group_id
  kms_key_arn           = module.secrets.kms_key_arn
  db_name               = var.postgres_database_name
  engine_version        = var.postgres_engine_version
  instance_class        = local.postgres_instance_class
  allocated_storage     = local.postgres_allocated_storage
  multi_az              = var.environment == "prod"
  backup_retention      = var.environment == "prod" ? 35 : 7
}

module "documentdb" {
  source               = "./modules/documentdb"
  deployment_name      = var.deployment_name
  vpc_id               = module.network.vpc_id
  db_subnet_ids        = module.network.private_db_subnet_ids
  db_security_group_id = module.network.data_security_group_id
  kms_key_arn          = module.secrets.kms_key_arn
  instance_class       = local.documentdb_instance_class
  environment          = var.environment
}

module "elasticache" {
  source               = "./modules/elasticache"
  deployment_name      = var.deployment_name
  vpc_id               = module.network.vpc_id
  subnet_ids           = module.network.private_db_subnet_ids
  db_security_group_id = module.network.data_security_group_id
  kms_key_arn          = module.secrets.kms_key_arn
  node_type            = local.redis_node_type
  environment          = var.environment
}

module "fargate_worker" {
  source                = "./modules/fargate-worker"
  deployment_name       = var.deployment_name
  environment           = var.environment
  region                = var.region
  private_subnet_ids    = module.network.private_app_subnet_ids
  app_security_group_id = module.network.app_security_group_id
  cluster_name_suffix   = "worker"
  service_name          = "trimcp"
  # --- Orchestrator (control plane — full data-plane access) ---
  container_image       = var.worker_container_image
  cpu                   = var.worker_cpu
  memory                = var.worker_memory
  desired_count         = var.worker_desired_count
  secrets_arns = compact([
    module.rds_postgres.secret_arn,
    module.documentdb.secret_arn,
    module.elasticache.auth_secret_arn,
  ])
  s3_bucket_arn = module.s3.bucket_arn
  # --- Worker (restricted — untrusted MCP integration execution) ---
  worker_container_image = var.worker_container_image
  worker_cpu             = var.worker_cpu
  worker_memory          = var.worker_memory
  worker_desired_count   = 1
  worker_s3_prefix       = "worker/"
  worker_secrets_arns    = []  # no Secrets Manager access for untrusted workers
}

module "webhook_api" {
  source          = "./modules/api-gateway-webhook"
  deployment_name = var.deployment_name
}

module "monitoring" {
  source          = "./modules/monitoring"
  deployment_name = var.deployment_name
  worker_log_group = module.fargate_worker.log_group_name
}
