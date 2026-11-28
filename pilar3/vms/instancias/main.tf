# La plantilla más nueva de las que creó vms/ (cada versión de la imagen es una plantilla nueva).
data "google_compute_instance_template" "minero" {
  filter      = "name eq minero-cpu-.*"
  most_recent = true
}

# El nombre es también el MINERO_ID (el alias del minero), y el escalador da de alta la cuenta de cada VM con ese
# nombre (un alta delegada en la cadena): así la reconoce en nodo:<cuenta>.
# BC-95: la clave la genera la VM al arrancar (arrancar.sh) y publica sólo la pública. Ni el escalador ni este state
# tienen privadas de las VMs.
resource "google_compute_instance_from_template" "minero" {
  count                    = var.cantidad
  name                     = "minero-cpu-${count.index}"
  source_instance_template = data.google_compute_instance_template.minero.self_link_unique
}
