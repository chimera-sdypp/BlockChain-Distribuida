# Migración de BC-109: estos recursos ya existen en GCP y los manejaban infra/ y vms/; los import los
# pasan a este state sin recrearlos. Se aplica una sola vez y después se borra este archivo: en un
# proyecto nuevo (donde no existen) el import fallaría. Ver pilar3/README.md, «Permisos».

locals {
  email_escalador = "escalador@${var.proyecto}.iam.gserviceaccount.com"
  email_minero    = "minero-cpu@${var.proyecto}.iam.gserviceaccount.com"
}

import {
  to = google_service_account.escalador
  id = "projects/${var.proyecto}/serviceAccounts/${local.email_escalador}"
}

import {
  to = google_service_account.minero
  id = "projects/${var.proyecto}/serviceAccounts/${local.email_minero}"
}

import {
  for_each = toset(["roles/artifactregistry.reader", "roles/logging.logWriter"])
  to       = google_project_iam_member.minero[each.value]
  id       = "${var.proyecto} ${each.value} serviceAccount:${local.email_minero}"
}

import {
  to = google_service_account_iam_member.escalador_usa_minero
  id = "projects/${var.proyecto}/serviceAccounts/${local.email_minero} roles/iam.serviceAccountUser serviceAccount:${local.email_escalador}"
}
