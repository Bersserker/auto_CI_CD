resource "yandex_kubernetes_node_group" "workers" {
  for_each    = var.node_groups
  cluster_id  = yandex_kubernetes_cluster.regional.id
  name        = "${var.name}-${each.key}"
  version     = var.master_version
  node_labels = { workload = each.value.gpus > 0 ? "gpu" : "cpu" }
  node_taints = each.value.gpus > 0 ? ["nvidia.com/gpu=present:NoSchedule"] : []
  lifecycle {
    precondition {
      condition     = contains(keys(var.subnets), each.value.zone)
      error_message = "Зона node group должна присутствовать в subnets."
    }
  }
  instance_template {
    platform_id = each.value.platform_id
    network_interface {
      nat                = true
      subnet_ids         = [var.subnets[each.value.zone].subnet_id]
      security_group_ids = [yandex_vpc_security_group.k8s.id]
    }
    resources {
      cores  = each.value.cores
      memory = each.value.memory
      gpus   = each.value.gpus
    }
    boot_disk {
      type = "network-ssd"
      size = each.value.disk_size
    }
    scheduling_policy { preemptible = false }
  }
  scale_policy {
    auto_scale {
      min     = each.value.min
      max     = each.value.max
      initial = each.value.initial
    }
  }
  allocation_policy {
    location { zone = each.value.zone }
  }
}
