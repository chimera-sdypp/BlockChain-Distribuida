variable "proyecto" {
  description = "ID del proyecto de GCP"
  type        = string
}

variable "region" {
  type    = string
  default = "us-central1"
}

# Cluster zonal: el primero de la cuenta no paga el cargo de administración de GKE.
variable "zona" {
  type    = string
  default = "us-central1-a"
}

variable "nodos_infra" {
  description = "Nodos del nodegroup de infra: RabbitMQ x3, Redis x3, Traefik y KEDA (los requests suman ~0,5 vCPU)"
  type        = number
  default     = 1
}

variable "nodos_apps" {
  description = "Nodos del nodegroup de apps: frontend, backend, nct, trp y gateway (x2) y el escalador (~0,5 vCPU)"
  type        = number
  default     = 1
}

variable "maquina" {
  type    = string
  default = "e2-standard-2"
}

# Disco estándar y chico: la quota de SSD de la cuenta de prueba es baja.
# Spot: ~70 % más barato que el normal. Si GCP se lleva el nodo, GKE crea otro y los pods vuelven
# (RabbitMQ y Redis con sus discos). Para una demo de la cátedra alcanza.
variable "spot" {
  type    = bool
  default = true
}

variable "disco_gb" {
  type    = number
  default = 30
}

variable "namespace" {
  description = "Namespace de Kubernetes del sistema (el del escalador)"
  type        = string
  default     = "blockchain"
}
