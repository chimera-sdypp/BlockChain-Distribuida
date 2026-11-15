# La plantilla más nueva de las que creó vms/ (cada versión de la imagen es una plantilla nueva).
data "google_compute_instance_template" "minero" {
  filter      = "name eq minero-cpu-.*"
  most_recent = true
}

# El nombre es también el MINERO_ID (el alias del minero), y el escalador aprueba la cuenta de cada VM
# con ese nombre en `cuentas`: así la reconoce en nodo:<cuenta>.
# La clave privada de cada VM la genera el escalador y llega por metadata (`clave-minero`); se suma a
# la de la plantilla, que trae el startup-script.
resource "google_compute_instance_from_template" "minero" {
  count                    = var.cantidad
  name                     = "minero-cpu-${count.index}"
  source_instance_template = data.google_compute_instance_template.minero.self_link_unique
  metadata = merge(data.google_compute_instance_template.minero.metadata, {
    clave-minero = lookup(var.claves, "minero-cpu-${count.index}", "")
  })
}
