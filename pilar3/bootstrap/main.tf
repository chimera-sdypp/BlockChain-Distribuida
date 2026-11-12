locals {
  apis = [
    "compute.googleapis.com",
    "container.googleapis.com",
    "artifactregistry.googleapis.com",
    "secretmanager.googleapis.com",
    "iam.googleapis.com",
    "iamcredentials.googleapis.com",
    "sts.googleapis.com",
    "cloudresourcemanager.googleapis.com",
  ]
}

resource "google_project_service" "apis" {
  for_each           = toset(local.apis)
  service            = each.value
  disable_on_destroy = false
}

# Imágenes de frontend, backend, nct, trp, escalador, gateway y minero-cpu.
# Los nodos de GKE y las VMs las bajan con su cuenta de servicio: no hay pull secrets.
resource "google_artifact_registry_repository" "imagenes" {
  location      = var.region
  repository_id = "blockchain"
  format        = "DOCKER"
  depends_on    = [google_project_service.apis]
}

# El MINER_TOKEN de las VMs minero-cpu. Acá sólo el secreto vacío: el valor se carga con gcloud
# (pilar3/README.md) para que no quede en el state.
resource "google_secret_manager_secret" "miner_token" {
  secret_id = "miner-token"
  replication {
    auto {}
  }
  depends_on = [google_project_service.apis]
}

# ---------------------------------------------------------------- Workload Identity Federation
# GitHub Actions entra con su token OIDC (vence en 1 h) y nada más: no hay claves en GitHub Secrets.

resource "google_iam_workload_identity_pool" "github" {
  workload_identity_pool_id = "github"
  display_name              = "GitHub Actions"
  depends_on                = [google_project_service.apis]
}

resource "google_iam_workload_identity_pool_provider" "github" {
  workload_identity_pool_id          = google_iam_workload_identity_pool.github.workload_identity_pool_id
  workload_identity_pool_provider_id = "github"
  display_name                       = "GitHub OIDC"
  attribute_mapping = {
    "google.subject"       = "assertion.sub"
    "attribute.repository" = "assertion.repository"
    "attribute.ref"        = "assertion.ref"
  }
  # Sólo tokens de nuestro repo: cualquier otro repo de GitHub queda afuera.
  attribute_condition = "assertion.repository == \"${var.repositorio}\""
  oidc {
    issuer_uri = "https://token.actions.githubusercontent.com"
  }
}

resource "google_service_account" "ci" {
  account_id   = "ci-github"
  display_name = "Pipelines de GitHub Actions"
}

resource "google_service_account_iam_member" "ci_desde_github" {
  service_account_id = google_service_account.ci.name
  role               = "roles/iam.workloadIdentityUser"
  member             = "principalSet://iam.googleapis.com/${google_iam_workload_identity_pool.github.name}/attribute.repository/${var.repositorio}"
}

resource "google_project_iam_member" "ci" {
  for_each = toset(var.roles_ci)
  project  = var.proyecto
  role     = each.value
  member   = "serviceAccount:${google_service_account.ci.email}"
}

# El state de los demás módulos (infra/, vms/) vive en el mismo bucket.
data "google_storage_bucket" "tfstate" {
  name = "${var.proyecto}-tfstate"
}

# Admin sólo de este bucket: además del state, vms/ le da al escalador permiso sobre él (leer y cambiar su IAM).
resource "google_storage_bucket_iam_member" "ci_tfstate" {
  bucket = data.google_storage_bucket.tfstate.name
  role   = "roles/storage.admin"
  member = "serviceAccount:${google_service_account.ci.email}"
}
