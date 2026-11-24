resource "yandex_kubernetes_cluster" "regional" {
  name                    = var.name
  folder_id               = var.folder_id
  network_id              = var.network_id
  network_policy_provider = "CALICO"
  cluster_ipv4_ranges     = [var.pod_cidr]
  service_ipv4_range      = "10.96.0.0/16"
  service_account_id      = yandex_iam_service_account.cluster.id
  node_service_account_id = yandex_iam_service_account.nodes.id
  master {
    master_logging {
      log_group_id               = var.log_group_id
      enabled                    = true
      kube_apiserver_enabled     = true
      audit_enabled              = true
      events_enabled             = true
      cluster_autoscaler_enabled = true
    }
    version   = var.master_version
    public_ip = var.master_public_ip
    regional {
      region = "ru-central1"
      dynamic "location" {
        for_each = var.subnets
        content {
          zone      = location.value.zone
          subnet_id = location.value.subnet_id
        }
      }
    }
    security_group_ids = [yandex_vpc_security_group.k8s.id]
  }
  kms_provider { key_id = yandex_kms_symmetric_key.secrets.id }
  depends_on = [
    yandex_resourcemanager_folder_iam_member.cluster,
    yandex_resourcemanager_folder_iam_member.puller,
    yandex_kms_symmetric_key_iam_binding.cluster
  ]
}
