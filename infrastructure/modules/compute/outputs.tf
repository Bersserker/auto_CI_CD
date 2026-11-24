output "external_ip" {
  value = yandex_compute_instance.nekon.network_interface[0].nat_ip_address
}
output "instance_id" { value = yandex_compute_instance.nekon.id }
