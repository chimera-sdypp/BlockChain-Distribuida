# Pipeline 4 (BC-35): la plantilla de las VMs minero-cpu, su firewall y sus permisos.
# Las VMs en sí están en instancias/, con otro state: las maneja el escalador, no el pipeline.
#   tofu init -backend-config="bucket=<proyecto>-tfstate"
terraform {
  required_version = ">= 1.8"

  backend "gcs" {
    prefix = "vms"
  }

  required_providers {
    google = {
      source  = "hashicorp/google"
      version = "~> 6.0"
    }
  }
}

provider "google" {
  project = var.proyecto
  region  = var.region
}
