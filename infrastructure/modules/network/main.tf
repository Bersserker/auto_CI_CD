resource "yandex_vpc_network" "test-vpc" {
  name = var.name
}
resource "yandex_vpc_subnet" "test-subnet" {
  name           = "${var.name}-subnet"
  zone           = var.zone
  v4_cidr_blocks = [var.subnet_cidr]
  network_id     = yandex_vpc_network.test-vpc.id
}
resource "yandex_vpc_security_group" "test-sg" {
  name       = "${var.name}-sg"
  network_id = yandex_vpc_network.test-vpc.id
  dynamic "ingress" {
    for_each = toset([80, 8080])
    content {
      protocol       = "TCP"
      port           = ingress.value
      v4_cidr_blocks = ["0.0.0.0/0"]
    }
  }
  dynamic "ingress" {
    for_each = length(var.ssh_allowed_cidrs) > 0 ? [1] : []
    content {
      protocol       = "TCP"
      port           = 22
      v4_cidr_blocks = var.ssh_allowed_cidrs
    }
  }
  egress {
    protocol       = "ANY"
    v4_cidr_blocks = ["0.0.0.0/0"]
  }
}

resource "yandex_vpc_subnet" "additional" {
  for_each       = var.additional_subnets
  name           = "${var.name}-${each.key}"
  zone           = each.value.zone
  network_id     = yandex_vpc_network.test-vpc.id
  v4_cidr_blocks = [each.value.cidr]
}
