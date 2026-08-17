variable "deployment_name" {
  type = string
}

variable "region" {
  type = string
}

variable "vpc_connector_id" {
  type = string
}

variable "service_account_email" {
  type = string
}

variable "image" {
  type = string
}

variable "postgres_secret_id" {
  type = string
}

variable "redis_secret_id" {
  type = string
}

variable "mongo_secret_id" {
  type = string
}

variable "gcs_bucket_name" {
  type = string
}
