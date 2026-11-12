variable "proyecto" {
  description = "ID del proyecto de GCP (cuenta de prueba de USD 300)"
  type        = string
}

variable "region" {
  description = "Región del Artifact Registry (la misma del cluster)"
  type        = string
  default     = "us-central1"
}

variable "repositorio" {
  description = "Repo de GitHub que puede entrar por OIDC (dueño/nombre)"
  type        = string
  default     = "chimera-sdypp/BlockChain-Distribuida"
}

# Lo que hace la cuenta «ci» en los 4 pipelines: crear la red, el cluster y las VMs (1 y 4),
# las cuentas de servicio de los nodos, del escalador y de las VMs con sus permisos (1 y 4),
# desplegar en GKE (2 y 3) y subir imágenes (3 y 4). El state lo escribe por el permiso del bucket.
variable "roles_ci" {
  description = "Roles de la cuenta de servicio de los pipelines"
  type        = list(string)
  default = [
    "roles/compute.networkAdmin",
    "roles/compute.securityAdmin",
    "roles/compute.instanceAdmin.v1",
    "roles/container.admin",
    "roles/iam.serviceAccountAdmin",
    "roles/iam.serviceAccountUser",
    "roles/resourcemanager.projectIamAdmin",
    "roles/artifactregistry.writer",
    "roles/secretmanager.admin",
  ]
}
