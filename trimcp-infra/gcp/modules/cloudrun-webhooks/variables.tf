variable "project_id" {
  type = string
}

variable "deployment_name" {
  type = string
}

variable "region" {
  type = string
}

variable "image" {
  type = string
}

variable "allow_unauthenticated_invoke" {
  type        = bool
  default     = false
  description = "Grant roles/run.invoker to allUsers. Prefer false + HTTPS LB + Cloud Armor (I.6)."
}
