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
  description = "Mínimo de nodos del nodegroup de infra (Cluster Autoscaler, BC-101)"
  type        = number
  default     = 1
}

variable "nodos_infra_max" {
  description = "Máximo de nodos del nodegroup de infra (quota 8 IPs públicas: 2 infra + 3 apps + 2 LB + VMs)"
  type        = number
  default     = 2
}

variable "nodos_apps" {
  description = "Mínimo de nodos del nodegroup de apps (Cluster Autoscaler, BC-101)"
  type        = number
  default     = 1
}

variable "nodos_apps_max" {
  description = "Máximo de nodos del nodegroup de apps (Cluster Autoscaler, BC-101)"
  type        = number
  default     = 3
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
