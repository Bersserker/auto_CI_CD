mock_provider "yandex" {}
variables {
  folder_id   = "mock-folder"
  bucket_name = "mock-private-artifacts"
}
run "versioned_private_storage" {
  command = plan
  assert {
    condition     = yandex_storage_bucket.this.versioning[0].enabled && !yandex_storage_bucket.this.force_destroy
    error_message = "State и артефакты должны версионироваться и не удаляться вместе с непустым бакетом."
  }
  assert {
    condition     = alltrue([for f in yandex_storage_bucket.this.anonymous_access_flags : !f.read && !f.list && !f.config_read])
    error_message = "Анонимный доступ должен быть запрещен."
  }
  assert {
    condition     = jsondecode(yandex_storage_bucket_policy.tls.policy).Statement[0].Effect == "Deny" && jsondecode(yandex_storage_bucket_policy.tls.policy).Statement[0].Condition.Bool["aws:SecureTransport"] == "false"
    error_message = "Незашифрованные HTTP-запросы должны запрещаться политикой."
  }
}
run "reject_invalid_bucket_name" {
  command = plan
  variables { bucket_name = "INVALID BUCKET" }
  expect_failures = [var.bucket_name]
}
