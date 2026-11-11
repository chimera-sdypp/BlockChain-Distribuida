data "google_compute_network" "vpc" {
  name = "blockchain" # la de infra/
}

data "google_storage_bucket" "tfstate" {
  name = "${var.proyecto}-tfstate"
}

# Creada por infra/; su pod corre el módulo instancias/.
data "google_service_account" "escalador" {
  account_id = "escalador"
}

# ---------------------------------------------------------------- Cuenta de las VMs
# Sólo puede bajar la imagen, leer el MINER_TOKEN y escribir logs.
resource "google_service_account" "minero" {
  account_id   = "minero-cpu"
  display_name = "VMs minero-cpu"
}

resource "google_project_iam_member" "minero" {
  for_each = toset(["roles/artifactregistry.reader", "roles/logging.logWriter"])
  project  = var.proyecto
  role     = each.value
  member   = "serviceAccount:${google_service_account.minero.email}"
}

resource "google_secret_manager_secret_iam_member" "minero_token" {
  secret_id = "miner-token" # creado por bootstrap/
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.minero.email}"
}

# ---------------------------------------------------------------- Firewall
# La VPC ya niega todo lo que entra; esta regla lo deja explícito para las VMs mineras.
# El minero sólo sale hacia el gateway (HTTP a su IP pública).
resource "google_compute_firewall" "minero_sin_entrada" {
  name          = "minero-cpu-sin-entrada"
  network       = data.google_compute_network.vpc.id
  direction     = "INGRESS"
  priority      = 100
  target_tags   = ["minero-cpu"]
  source_ranges = ["0.0.0.0/0"]
  deny {
    protocol = "all"
  }
}

# ---------------------------------------------------------------- Plantilla
resource "google_compute_instance_template" "minero" {
  name_prefix  = "minero-cpu-"
  machine_type = var.maquina
  tags         = ["minero-cpu"]

  disk {
    source_image = "cos-cloud/cos-stable"
    disk_type    = "pd-standard"
    disk_size_gb = 10
    boot         = true
    auto_delete  = true
  }

  network_interface {
    subnetwork = "projects/${var.proyecto}/regions/${var.region}/subnetworks/blockchain-nodos"
    access_config {} # IP pública efímera: para salir a Internet sin Cloud NAT
  }

  service_account {
    email  = google_service_account.minero.email
    scopes = ["cloud-platform"]
  }

  metadata = {
    startup-script = templatefile("${path.module}/arrancar.sh", {
      imagen      = var.imagen
      gateway_url = var.gateway_url
      proyecto    = var.proyecto
      registro    = split("/", var.imagen)[0]
    })
  }

  # Una plantilla no se edita: se crea la nueva y después se borra la vieja.
  lifecycle {
    create_before_destroy = true
  }
}

# ---------------------------------------------------------------- Permisos del escalador
# Crear y borrar instancias, ponerles la cuenta minero-cpu y escribir el state de instancias/.
resource "google_project_iam_member" "escalador_instancias" {
  project = var.proyecto
  role    = "roles/compute.instanceAdmin.v1"
  member  = data.google_service_account.escalador.member
}

resource "google_service_account_iam_member" "escalador_usa_minero" {
  service_account_id = google_service_account.minero.name
  role               = "roles/iam.serviceAccountUser"
  member             = data.google_service_account.escalador.member
}

resource "google_storage_bucket_iam_member" "escalador_tfstate" {
  bucket = data.google_storage_bucket.tfstate.name
  role   = "roles/storage.objectAdmin"
  member = data.google_service_account.escalador.member
}
