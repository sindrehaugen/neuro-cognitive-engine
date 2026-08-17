variable "deployment_name" {
  type = string
}

variable "region" {
  type = string
}

variable "vpc_cidr" {
  type = string
}

variable "vpn_subnet_cidr" {
  type        = string
  description = "Documented VPN client CIDR; optionally peered — no public DB exposure (Appendix I.6)"
}

variable "availability_zones" {
  type = list(string)
}
