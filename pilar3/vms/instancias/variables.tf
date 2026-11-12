variable "proyecto" {
  type = string
}

variable "zona" {
  type    = string
  default = "us-central1-a"
}

variable "cantidad" {
  description = "Cuántas VMs minero-cpu tiene que haber (la decide el escalador)"
  type        = number
  default     = 0
  validation {
    condition     = var.cantidad >= 0 && var.cantidad <= 10
    error_message = "cantidad tiene que estar entre 0 y 10."
  }
}
