data "yandex_compute_image" "ubuntu" {
  family = var.image_family
}
resource "yandex_vpc_address" "test-ip" {
  name = "${var.name}-ip"
  external_ipv4_address { zone_id = var.zone }
}
resource "yandex_compute_instance" "nekon" {
  name        = var.name
  zone        = var.zone
  platform_id = "standard-v3"
  resources {
    cores  = var.cores
    memory = var.memory
  }
  boot_disk {
    initialize_params {
      image_id = data.yandex_compute_image.ubuntu.id
      size     = 20
    }
  }
  network_interface {
    subnet_id          = var.subnet_id
    security_group_ids = [var.security_group_id]
    nat                = true
    nat_ip_address     = yandex_vpc_address.test-ip.external_ipv4_address[0].address
  }
  metadata = {
    ssh-keys  = "ubuntu:${trimspace(file(pathexpand(var.ssh_public_key_path)))}"
    user-data = file("${path.module}/init.sh")
  }
}
