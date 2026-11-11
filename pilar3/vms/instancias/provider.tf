# Las VMs minero-cpu-0…N-1. Lo aplica el escalador (BC-29) desde su pod, no un pipeline:
#   tofu apply -var cantidad=N
# Por eso tiene su propio state: un apply del Pipeline 4 no puede borrar las VMs que levantó él.
# En el pod, el bucket y las variables llegan por entorno:
#   TF_CLI_ARGS_init="-backend-config=bucket=<proyecto>-tfstate"  TF_VAR_proyecto  TF_VAR_zona
terraform {
  required_version = ">= 1.8"

  backend "gcs" {
    prefix = "vms-instancias"
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
  zone    = var.zona
}
