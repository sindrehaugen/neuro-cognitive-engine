terraform {
  required_version = ">= 1.6.0"
  # Production: configure remote state (example — adjust bucket/prefix per org):
  # backend "gcs" {
  #   bucket = "your-terraform-state-bucket"
  #   prefix = "trimcp/gcp"
  # }
  required_providers {
    google = {
      source  = "hashicorp/google"
      version = "~> 5.45"
    }
    random = {
      source  = "hashicorp/random"
      version = "~> 3.6"
    }
  }
}

provider "google" {
  project = var.project_id
  region  = var.region
}

module "network" {
  source            = "./modules/network"
  project_id        = var.project_id
  deployment_name   = var.deployment_name
  region            = var.region
  vpc_cidr          = var.vpc_cidr
  labels            = local.common_labels
}

module "cloudsql" {
  source                       = "./modules/cloudsql"
  deployment_name              = var.deployment_name
  region                       = var.region
  environment                  = var.environment
  network_id                   = module.network.network_id
  tier                         = var.postgres_tier
  disk_size_gb                 = var.postgres_disk_gb
  labels                       = local.common_labels
  worker_service_account_email = module.network.worker_sa_email

  depends_on = [module.network]
}

module "memorystore" {
  source                       = "./modules/memorystore"
  deployment_name              = var.deployment_name
  region                       = var.region
  network_id                   = module.network.network_id
  memory_size_gb               = local.redis_memory
  labels                       = local.common_labels
  app_cidr_blocks              = [module.network.subnet_cidr_app]
  worker_service_account_email = module.network.worker_sa_email

  depends_on = [module.network]
}

module "mongo" {
  source                       = "./modules/mongo-secret"
  deployment_name              = var.deployment_name
  worker_service_account_email = module.network.worker_sa_email
}

module "gcs" {
  source                       = "./modules/gcs"
  project_id                   = var.project_id
  deployment_name              = var.deployment_name
  region                       = var.region
  labels                       = local.common_labels
  worker_service_account_email = module.network.worker_sa_email
}

module "cloudrun_worker" {
  source                = "./modules/cloudrun-worker"
  deployment_name     = var.deployment_name
  region             = var.region
  vpc_connector_id   = module.network.vpc_connector_id
  service_account_email = module.network.worker_sa_email
  image              = var.worker_image
  postgres_secret_id = module.cloudsql.admin_secret_id
  redis_secret_id    = module.memorystore.auth_secret_id
  mongo_secret_id    = module.mongo.connection_secret_id
  gcs_bucket_name    = module.gcs.bucket_name

  depends_on = [module.cloudsql, module.memorystore, module.gcs, module.mongo]
}

module "cloudrun_webhooks" {
  source          = "./modules/cloudrun-webhooks"
  project_id      = var.project_id
  deployment_name = var.deployment_name
  region          = var.region
  image           = var.webhook_image
}

module "monitoring" {
  source          = "./modules/monitoring"
  project_id      = var.project_id
  deployment_name = var.deployment_name
}
