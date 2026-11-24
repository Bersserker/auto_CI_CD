mock_provider "yandex" {}
variables {
  name           = "mock-cluster"
  folder_id      = "mock-folder"
  network_id     = "mock-network"
  master_version = "1.33"
  log_group_id   = "mock-log-group"
  subnets = {
    ru-central1-a = { zone = "ru-central1-a", subnet_id = "mock-a", cidr = "10.20.0.0/24" }
    ru-central1-b = { zone = "ru-central1-b", subnet_id = "mock-b", cidr = "10.20.1.0/24" }
    ru-central1-d = { zone = "ru-central1-d", subnet_id = "mock-d", cidr = "10.20.2.0/24" }
  }
  node_groups = {
    cpu = { zone = "ru-central1-a" }
    gpu = { zone = "ru-central1-a", platform_id = "gpu-standard-v2", cores = 8, memory = 48, gpus = 1 }
  }
}
run "cluster_security_and_gpu" {
  command = plan
  assert {
    condition     = yandex_kubernetes_cluster.regional.network_policy_provider == "CALICO" && !yandex_kubernetes_cluster.regional.master[0].public_ip
    error_message = "Кластер должен поддерживать NetworkPolicy и иметь приватный API по умолчанию."
  }
  assert {
    condition     = length(yandex_kubernetes_cluster.regional.master[0].regional[0].location) == 3
    error_message = "Master должен размещаться в трех зонах."
  }
  assert {
    condition     = yandex_kubernetes_node_group.workers["gpu"].instance_template[0].resources[0].gpus == 1 && contains(yandex_kubernetes_node_group.workers["gpu"].node_taints, "nvidia.com/gpu=present:NoSchedule")
    error_message = "GPU-узел должен иметь GPU и быть изолирован от обычных workloads taint-ом."
  }
  assert {
    condition     = yandex_kubernetes_node_group.workers["cpu"].instance_template[0].resources[0].gpus == 0
    error_message = "CPU-группа не должна запрашивать GPU."
  }
  assert {
    condition     = yandex_kms_symmetric_key_iam_binding.cluster.role == "kms.keys.encrypterDecrypter"
    error_message = "Для шифрования секретов недостаточно роли viewer."
  }
  assert {
    condition     = yandex_kubernetes_cluster.regional.master[0].master_logging[0].audit_enabled
    error_message = "Аудит control plane должен быть включен."
  }
}
run "reject_invalid_autoscaling" {
  command = plan
  variables {
    node_groups = { cpu = { zone = "ru-central1-a", min = 3, initial = 1, max = 2 } }
  }
  expect_failures = [var.node_groups]
}
run "reject_two_master_zones" {
  command = plan
  variables {
    subnets = {
      ru-central1-a = { zone = "ru-central1-a", subnet_id = "mock-a", cidr = "10.20.0.0/24" }
      ru-central1-b = { zone = "ru-central1-b", subnet_id = "mock-b", cidr = "10.20.1.0/24" }
    }
  }
  expect_failures = [var.subnets]
}
