variable "proyecto" {
  description = "ID del proyecto de GCP"
  type        = string
}

variable "region" {
  type    = string
  default = "us-central1"
}

variable "imagen" {
  description = "Imagen del minero CPU en Artifact Registry, con tag (la sube el Pipeline 4)"
  type        = string
}

variable "gateway_url" {
  description = "URL pública del gateway (su LoadBalancer en GKE, puerto 8081)"
  type        = string
}

variable "maquina" {
  type    = string
  default = "e2-highcpu-2"
}
