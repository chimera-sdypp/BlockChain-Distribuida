data "google_compute_network" "vpc" {
  name = "blockchain" # la de infra/
}

data "google_storage_bucket" "tfstate" {
  name = "${var.proyecto}-tfstate"
}

# Las dos cuentas las crea bootstrap/ (BC-111). El pod del escalador corre el módulo instancias/.
data "google_service_account" "escalador" {
  account_id = "escalador"
}

# La de las VMs: sólo puede bajar la imagen y escribir logs. La clave del minero la genera la propia VM (BC-95).
data "google_service_account" "minero" {
  account_id = "minero-cpu"
}

# Antes las creaba este módulo: se olvidan del state sin borrarlas (ahora las maneja bootstrap/).
removed {
  from = google_service_account.minero
  lifecycle {
    destroy = false
  }
}

removed {
  from = google_project_iam_member.minero
  lifecycle {
    destroy = false
  }
}

removed {
  from = google_service_account_iam_member.escalador_usa_minero
  lifecycle {
    destroy = false
  }
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

  # Spot: si GCP la reclama se borra, y el escalador la vuelve a crear en su próximo apply.
  scheduling {
    provisioning_model          = "SPOT"
    preemptible                 = true
    automatic_restart           = false
    instance_termination_action = "DELETE"
  }

  service_account {
    email  = data.google_service_account.minero.email
    scopes = ["cloud-platform"]
  }

  metadata = {
    # BC-95: la VM publica su clave pública en sus guest attributes (sólo ella puede escribirlos) y el escalador la
    # lee de ahí (compute.instances.getGuestAttributes, que está en compute.instanceAdmin.v1).
    enable-guest-attributes = "TRUE"
    startup-script = templatefile("${path.module}/arrancar.sh", {
      imagen      = var.imagen
      gateway_url = var.gateway_url
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

resource "google_storage_bucket_iam_member" "escalador_tfstate" {
  bucket = data.google_storage_bucket.tfstate.name
  role   = "roles/storage.objectAdmin"
  member = data.google_service_account.escalador.member
}
