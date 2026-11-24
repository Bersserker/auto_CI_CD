mock_provider "yandex" {}
variables {
  cloud_id                 = "mock-cloud"
  folder_id                = "mock-folder"
  service_account_key_file = "/mock/key.json"
  state_bucket_name        = "mock-terraform-state"
}
run "bootstrap_state" {
  command = plan
  assert {
    condition     = module.state.bucket_name == "mock-terraform-state"
    error_message = "Bootstrap должен создавать запрошенный state-бакет."
  }
}
