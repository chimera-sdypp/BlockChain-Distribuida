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

# La IP fija del LoadBalancer del gateway (el Pipeline 3 la pone en el Service). Los mineros no la usan: entran por
# GATEWAY_URL, con un nombre de DuckDNS que apunta a esta IP y el certificado del Secret gateway-tls (BC-105).
output "GATEWAY_IP" {
  value = google_compute_address.gateway.address
}

output "GATEWAY_URL" {
  value = "https://chimera-gateway.duckdns.org"
}

output "INGRESO_IP" {
  value = google_compute_address.ingreso.address
}
