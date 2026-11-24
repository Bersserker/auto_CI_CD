mock_provider "yandex" {}
variables {
  enable_compute           = false
  enable_kubernetes        = true
  enable_gpu               = true
  cloud_id                 = "mock-cloud"
  folder_id                = "mock-folder"
  service_account_key_file = "/mock/not-a-real-key.json"
  artifacts_bucket_name    = "mock-staging-artifacts"
  kubernetes_version       = "1.33"
}
run "full_infrastructure" {
  command = plan
  assert {
    condition     = length(module.network.kubernetes_subnets) == 3
    error_message = "Региональный master должен получить три подсети."
  }
  assert {
    condition     = length(module.kubernetes[0].node_group_ids) == 2
    error_message = "Отсутствует CPU/GPU node group."
  }
  assert {
    condition     = contains(keys(module.kubernetes[0].node_group_ids), "gpu")
    error_message = "Полный этап 2 требует GPU-группу."
  }
  assert {
    condition     = length(module.compute) == 0
    error_message = "Учебная VM не должна создаваться по умолчанию."
  }
  assert {
    condition     = module.storage.bucket_name == "mock-staging-artifacts"
    error_message = "Бакет артефактов должен относиться к своему окружению."
  }
}
run "cpu_only_development" {
  command = plan
  variables { enable_gpu = false }
  assert {
    condition     = !contains(keys(module.kubernetes[0].node_group_ids), "gpu")
    error_message = "Отключение GPU должно убрать GPU-группу."
  }
}
run "without_cluster" {
  command = plan
  variables { enable_kubernetes = false }
  assert {
    condition     = length(module.kubernetes) == 0 && length(module.monitoring) == 0
    error_message = "Отключение кластера должно отключать и его Monitoring."
  }
}
run "reject_unpinned_version" {
  command = plan
  variables { kubernetes_version = "latest" }
  expect_failures = [var.kubernetes_version]
}
