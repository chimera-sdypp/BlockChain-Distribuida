# Pipeline 1 (BC-32): la red y el cluster GKE con sus dos nodegroups.
#   tofu init -backend-config="bucket=<proyecto>-tfstate"
terraform {
  required_version = ">= 1.8"

  backend "gcs" {
    prefix = "infra"
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
  zone    = var.zona
}
