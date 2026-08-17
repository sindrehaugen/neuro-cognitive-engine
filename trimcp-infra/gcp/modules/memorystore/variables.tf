variable "deployment_name" {
  type = string
}

variable "region" {
  type = string
}

variable "network_id" {
  type = string
}

variable "memory_size_gb" {
  type = number
}

variable "labels" {
  type    = map(string)
  default = {}
}

variable "app_cidr_blocks" {
  type        = list(string)
  description = "Reserved for future Redis ACL documentation (I.6); DIRECT_PEERING uses VPC-only access"
}

variable "worker_service_account_email" {
  type        = string
  description = "Grant secretAccessor on the Redis auth secret only"
}
