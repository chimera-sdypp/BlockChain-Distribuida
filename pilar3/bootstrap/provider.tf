# Base de GCP (BC-31): lo que los pipelines necesitan para entrar. Se aplica a mano, una sola vez.
# El bucket del state lo crea gcloud antes (ver pilar3/README.md): sin él no hay dónde guardar este state.
#   tofu init -backend-config="bucket=<proyecto>-tfstate"
terraform {
  required_version = ">= 1.8"

  backend "gcs" {
    prefix = "bootstrap"
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
