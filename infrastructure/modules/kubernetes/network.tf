resource "yandex_vpc_security_group" "k8s" {
  name       = "${var.name}-k8s"
  network_id = var.network_id
  ingress {
    protocol          = "ANY"
    predefined_target = "self_security_group"
  }
  ingress {
    protocol          = "TCP"
    predefined_target = "loadbalancer_healthchecks"
    from_port         = 0
    to_port           = 65535
  }
  ingress {
    protocol       = "ICMP"
    v4_cidr_blocks = [for s in values(var.subnets) : s.cidr]
  }
  dynamic "ingress" {
    for_each = length(var.api_allowed_cidrs) > 0 ? toset([443, 6443]) : toset([])
    content {
      protocol       = "TCP"
      port           = ingress.value
      v4_cidr_blocks = var.api_allowed_cidrs
    }
  }
  ingress {
    protocol       = "ANY"
    v4_cidr_blocks = [var.pod_cidr]
  }
  egress {
    protocol       = "ANY"
    v4_cidr_blocks = ["0.0.0.0/0"]
  }
}
