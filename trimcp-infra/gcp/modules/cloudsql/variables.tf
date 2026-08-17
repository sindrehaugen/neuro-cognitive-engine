variable "deployment_name" {
  type = string
}

variable "region" {
  type = string
}

variable "environment" {
  type = string
}

variable "network_id" {
  type = string
}

variable "tier" {
  type = string
}

variable "disk_size_gb" {
  type = number
}

variable "labels" {
  type = map(string)
}

variable "worker_service_account_email" {
  type        = string
  description = "Grant secretAccessor on the Postgres admin secret only"
}
