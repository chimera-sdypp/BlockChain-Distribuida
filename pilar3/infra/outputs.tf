output "cluster" {
  value = google_container_cluster.blockchain.name
}

output "zona" {
  value = google_container_cluster.blockchain.location
}

# Para la anotación iam.gke.io/gcp-service-account del ServiceAccount escalador.
output "cuenta_escalador" {
  value = google_service_account.escalador.email
}

output "conectarse" {
  value = "gcloud container clusters get-credentials ${google_container_cluster.blockchain.name} --zone ${var.zona} --project ${var.proyecto}"
}
