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

# IP pública fija del gateway de mineros: vive acá y no en infra/ para que sobreviva a un destroy
# del cluster (los mineros de afuera la tienen configurada). El Service la toma en el Pipeline 3.
resource "google_compute_address" "gateway" {
  name        = "gateway"
  region      = var.region
  description = "IP fija del gateway de mineros (LoadBalancer en GKE)"
  depends_on  = [google_project_service.apis]
}

# IP pública fija de Traefik (las páginas y la API): el nombre de DuckDNS apunta acá y el certificado
# de Let's Encrypt es para ese nombre, así que tiene que sobrevivir a un destroy del cluster.
resource "google_compute_address" "ingreso" {
  name        = "ingreso"
  region      = var.region
  description = "IP fija de Traefik (las paginas y la API, con HTTPS)"
  depends_on  = [google_project_service.apis]
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

# ---------------------------------------------------------------- Cuentas del escalador y de las VMs (BC-111)
# Viven acá y no en infra/ ni vms/ para que la cuenta de los pipelines no tenga que crear cuentas ni dar
# roles en el proyecto (BC-109). Un destroy de infra/ ya no se las lleva.

# Su ServiceAccount de Kubernetes (blockchain/escalador) actúa como esta cuenta: ese binding sí queda en
# infra/, porque necesita el pool de Workload Identity del cluster.
resource "google_service_account" "escalador" {
  account_id   = "escalador"
  display_name = "Escalador de VMs minero-cpu"
}

# La cuenta de las VMs: sólo puede bajar la imagen y escribir logs.
resource "google_service_account" "minero" {
  account_id   = "minero-cpu"
  display_name = "VMs minero-cpu"
}

resource "google_project_iam_member" "minero" {
  for_each = toset(["roles/artifactregistry.reader", "roles/logging.logWriter"])
  project  = var.proyecto
  role     = each.value
  member   = google_service_account.minero.member
}

# El escalador les pone la cuenta minero-cpu a las VMs que crea.
resource "google_service_account_iam_member" "escalador_usa_minero" {
  service_account_id = google_service_account.minero.name
  role               = "roles/iam.serviceAccountUser"
  member             = google_service_account.escalador.member
}

# infra/ maneja el binding de Workload Identity del escalador: la cuenta de los pipelines administra
# esa cuenta y ninguna otra.
resource "google_service_account_iam_member" "ci_administra_escalador" {
  service_account_id = google_service_account.escalador.name
  role               = "roles/iam.serviceAccountAdmin"
  member             = google_service_account.ci.member
}

# ---------------------------------------------------------------- Permisos del escalador (BC-111)
# Sólo sus VMs: crear, leer y borrar las instancias minero-cpu-* (y sus discos, que se llaman igual)
# desde la plantilla minero-cpu-*, en la subred de los nodos. Un nodo de GKE (gke-blockchain-…) no entra.
resource "google_project_iam_custom_role" "escalador_vms" {
  role_id     = "escaladorVms"
  title       = "Escalador: VMs minero-cpu"
  description = "Crear, leer y borrar las VMs minero-cpu-* desde su plantilla (BC-111). Va con condición por nombre."
  permissions = [
    "compute.instances.create",
    "compute.instances.delete",
    "compute.instances.get",
    "compute.instances.getGuestAttributes",
    "compute.instances.setLabels",
    "compute.instances.setMetadata",
    "compute.instances.setServiceAccount",
    "compute.instances.setTags",
    "compute.disks.create",
    "compute.disks.delete",
    "compute.instanceTemplates.get",
    "compute.instanceTemplates.useReadOnly",
    "compute.subnetworks.use",
    "compute.subnetworks.useExternalIp",
  ]
}

resource "google_project_iam_member" "escalador_vms" {
  project = var.proyecto
  role    = google_project_iam_custom_role.escalador_vms.id
  member  = google_service_account.escalador.member
  condition {
    title       = "solo-minero-cpu"
    description = "Instancias, discos y plantillas minero-cpu-*, y la subred blockchain-nodos"
    expression  = <<-CEL
      resource.name.extract("/instances/{nombre}").startsWith("minero-cpu-") ||
      resource.name.extract("/disks/{nombre}").startsWith("minero-cpu-") ||
      resource.name.extract("/instanceTemplates/{nombre}").startsWith("minero-cpu-") ||
      resource.name.extract("/subnetworks/{nombre}") == "blockchain-nodos"
    CEL
  }
}

# Listar no se puede acotar por nombre (la condición ve el proyecto, no cada VM): va aparte, sin condición.
# Con esto sólo ve nombres; para leer o tocar una VM hace falta el rol de arriba.
resource "google_project_iam_custom_role" "escalador_listar" {
  role_id     = "escaladorListar"
  title       = "Escalador: listar"
  description = "Listar instancias y plantillas y seguir las operaciones de zona (BC-111)."
  permissions = [
    "compute.instances.list",
    "compute.instanceTemplates.list",
    "compute.zoneOperations.get",
    "compute.zones.get",
  ]
}

resource "google_project_iam_member" "escalador_listar" {
  project = var.proyecto
  role    = google_project_iam_custom_role.escalador_listar.id
  member  = google_service_account.escalador.member
}

# Su state (vms-instancias/) y nada más: ni el de infra, ni el de vms, ni el de bootstrap. La condición
# también deja pasar el bucket mismo, porque tofu init y el lock listan los objetos del prefijo.
resource "google_storage_bucket_iam_member" "escalador_tfstate" {
  bucket = data.google_storage_bucket.tfstate.name
  role   = "roles/storage.objectAdmin"
  member = google_service_account.escalador.member
  condition {
    title       = "solo-vms-instancias"
    description = "El state del módulo vms/instancias"
    expression  = <<-CEL
      resource.name == "projects/_/buckets/${data.google_storage_bucket.tfstate.name}" ||
      resource.name.startsWith("projects/_/buckets/${data.google_storage_bucket.tfstate.name}/objects/vms-instancias/")
    CEL
  }
}
