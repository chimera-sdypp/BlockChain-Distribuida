# Lo que se carga como variables del repo en GitHub (Settings → Secrets and variables → Actions → Variables).
# No son secretos: sin el token OIDC de nuestro repo no sirven para nada.

output "GCP_PROYECTO" {
  value = var.proyecto
}

output "GCP_WIF_PROVEEDOR" {
  value = google_iam_workload_identity_pool_provider.github.name
}

output "GCP_CUENTA_CI" {
  value = google_service_account.ci.email
}

output "GCP_REGISTRO" {
  value = "${var.region}-docker.pkg.dev/${var.proyecto}/${google_artifact_registry_repository.imagenes.repository_id}"
}

output "GATEWAY_URL" {
  value = "http://${google_compute_address.gateway.address}:8081"
}
