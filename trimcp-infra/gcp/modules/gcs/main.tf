locals {
  name = replace("trimcp-${var.deployment_name}", "_", "-")
}

resource "google_storage_bucket" "blobs" {
  name                        = "${var.project_id}-${local.name}-blobs"
  location                    = var.region
  uniform_bucket_level_access = true
  force_destroy               = var.force_destroy

  labels = var.labels
}

resource "google_storage_bucket_iam_member" "worker" {
  bucket = google_storage_bucket.blobs.name
  role   = "roles/storage.objectAdmin"
  member = "serviceAccount:${var.worker_service_account_email}"
}

output "bucket_name" {
  value = google_storage_bucket.blobs.name
}

output "bucket_url" {
  value = google_storage_bucket.blobs.url
}
