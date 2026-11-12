# ---------------------------------------------------------------- Red
resource "google_compute_network" "vpc" {
  name                    = "blockchain"
  auto_create_subnetworks = false
}

resource "google_compute_subnetwork" "nodos" {
  name          = "blockchain-nodos"
  network       = google_compute_network.vpc.id
  region        = var.region
  ip_cidr_range = "10.10.0.0/20"

  secondary_ip_range {
    range_name    = "pods"
    ip_cidr_range = "10.20.0.0/16"
  }
  secondary_ip_range {
    range_name    = "servicios"
    ip_cidr_range = "10.30.0.0/20"
  }
}

# ---------------------------------------------------------------- Cluster
resource "google_container_cluster" "blockchain" {
  name     = "blockchain"
  location = var.zona

  network    = google_compute_network.vpc.id
  subnetwork = google_compute_subnetwork.nodos.id
  ip_allocation_policy {
    cluster_secondary_range_name  = "pods"
    services_secondary_range_name = "servicios"
  }

  # Los nodegroups van aparte: se borra el pool por defecto apenas se crea el cluster.
  remove_default_node_pool = true
  initial_node_count       = 1
  node_config {
    disk_type    = "pd-standard"
    disk_size_gb = var.disco_gb
  }

  # Los pods del escalador entran a GCP con la cuenta de servicio de abajo, sin claves.
  workload_identity_config {
    workload_pool = "${var.proyecto}.svc.id.goog"
  }

  release_channel {
    channel = "REGULAR"
  }

  # El Ingress lo atiende Traefik (Pipeline 2): sin esto, GKE además crearía su balanceador HTTP (pago).
  addons_config {
    http_load_balancing {
      disabled = true
    }
  }

  # Que un tofu destroy lo pueda borrar al terminar la evaluación.
  deletion_protection = false
}

# Cuenta de los nodos: logs, métricas y bajar imágenes del Artifact Registry.
resource "google_service_account" "nodos" {
  account_id   = "gke-nodos"
  display_name = "Nodos de GKE"
}

resource "google_project_iam_member" "nodos" {
  for_each = toset([
    "roles/logging.logWriter",
    "roles/monitoring.metricWriter",
    "roles/artifactregistry.reader",
  ])
  project = var.proyecto
  role    = each.value
  member  = "serviceAccount:${google_service_account.nodos.email}"
}

locals {
  nodegroups = {
    # Con taint: sólo entran los pods que lo toleran (RabbitMQ, Redis, Traefik, KEDA, observabilidad).
    infra = { nodos = var.nodos_infra, taint = true }
    apps  = { nodos = var.nodos_apps, taint = false }
  }
}

resource "google_container_node_pool" "nodegroup" {
  for_each   = local.nodegroups
  name       = each.key
  cluster    = google_container_cluster.blockchain.id
  node_count = each.value.nodos

  node_config {
    machine_type    = var.maquina
    disk_type       = "pd-standard"
    disk_size_gb    = var.disco_gb
    service_account = google_service_account.nodos.email
    oauth_scopes    = ["https://www.googleapis.com/auth/cloud-platform"]
    labels          = { nodegroup = each.key }

    dynamic "taint" {
      for_each = each.value.taint ? [1] : []
      content {
        key    = "nodegroup"
        value  = each.key
        effect = "NO_SCHEDULE"
      }
    }

    workload_metadata_config {
      mode = "GKE_METADATA"
    }
  }

  management {
    auto_repair  = true
    auto_upgrade = true
  }
}

# ---------------------------------------------------------------- Escalador (BC-29)
# Su ServiceAccount de Kubernetes (blockchain/escalador) actúa como esta cuenta de GCP.
# Los permisos sobre las VMs y su state se los da el módulo vms/ (Pipeline 4).
resource "google_service_account" "escalador" {
  account_id   = "escalador"
  display_name = "Escalador de VMs minero-cpu"
}

resource "google_service_account_iam_member" "escalador_desde_gke" {
  service_account_id = google_service_account.escalador.name
  role               = "roles/iam.workloadIdentityUser"
  member             = "serviceAccount:${var.proyecto}.svc.id.goog[${var.namespace}/escalador]"
  depends_on         = [google_container_cluster.blockchain]
}
