locals {
  name = replace("trimcp-${var.deployment_name}", "_", "-")
}

resource "google_compute_network" "vpc" {
  name                    = "${local.name}-vpc"
  auto_create_subnetworks = false
}

resource "google_compute_subnetwork" "app" {
  name          = "${local.name}-app"
  ip_cidr_range = cidrsubnet(var.vpc_cidr, 8, 1)
  region        = var.region
  network       = google_compute_network.vpc.id

  private_ip_google_access = true
}

# Range for Cloud SQL + Memorystore private service access (Appendix I.6 — no public DB IPs)
resource "google_compute_global_address" "private_peering" {
  name          = "${local.name}-psa"
  purpose       = "VPC_PEERING"
  address_type  = "INTERNAL"
  prefix_length = 20
  network       = google_compute_network.vpc.id
}

resource "google_service_networking_connection" "private_vpc_connection" {
  network                 = google_compute_network.vpc.id
  service                 = "servicenetworking.googleapis.com"
  reserved_peering_ranges = [google_compute_global_address.private_peering.name]
}

resource "google_compute_firewall" "allow_internal" {
  name    = "${local.name}-allow-internal"
  network = google_compute_network.vpc.name

  allow {
    protocol = "icmp"
  }
  allow {
    protocol = "tcp"
    ports    = ["443", "5432", "6379", "27017"]
  }
  allow {
    protocol = "udp"
    ports    = ["53"]
  }
  source_ranges = [var.vpc_cidr]
  priority      = 1000
}

resource "google_compute_router" "egress" {
  name    = "${local.name}-router"
  region  = var.region
  network = google_compute_network.vpc.id
}

resource "google_compute_router_nat" "egress" {
  name                               = "${local.name}-nat"
  router                             = google_compute_router.egress.name
  region                             = var.region
  nat_ip_allocate_option             = "AUTO_ONLY"
  source_subnetwork_ip_ranges_to_nat = "ALL_SUBNETWORKS_ALL_IP_RANGES"
}

resource "google_vpc_access_connector" "worker" {
  name          = "${local.name}-conn"
  region        = var.region
  network       = google_compute_network.vpc.id
  ip_cidr_range = cidrsubnet(var.vpc_cidr, 8, 10)
  min_instances = 2
  max_instances = 3
}

resource "google_service_account" "worker" {
  account_id   = "${substr(replace(local.name, "-", ""), 0, 20)}worker"
  display_name = "TriMCP worker / Cloud Run"
}

# Secret-level accessor grants live in cloudsql/memorystore/mongo-secret modules.

output "network_id" {
  value = google_compute_network.vpc.id
}

output "network_self_link" {
  value = google_compute_network.vpc.self_link
}

output "subnet_cidr_app" {
  value = google_compute_subnetwork.app.ip_cidr_range
}

output "app_subnet_self_link" {
  value = google_compute_subnetwork.app.self_link
}

output "db_subnet_self_link" {
  description = "Unused path (Cloud SQL uses PSA); kept for interface symmetry with Azure/AWS"
  value       = google_compute_subnetwork.app.self_link
}

output "vpc_connector_id" {
  value = google_vpc_access_connector.worker.id
}

output "worker_sa_email" {
  value = google_service_account.worker.email
}
