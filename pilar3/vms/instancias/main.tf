# La plantilla más nueva de las que creó vms/ (cada versión de la imagen es una plantilla nueva).
data "google_compute_instance_template" "minero" {
  filter      = "name eq minero-cpu-.*"
  most_recent = true
}

# El nombre es también el MINERO_ID: así el escalador reconoce a sus VMs en nodo:<id>.
resource "google_compute_instance_from_template" "minero" {
  count                    = var.cantidad
  name                     = "minero-cpu-${count.index}"
  source_instance_template = data.google_compute_instance_template.minero.self_link_unique
}
