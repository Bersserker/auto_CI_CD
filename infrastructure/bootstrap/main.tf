provider "yandex" {
  cloud_id                 = var.cloud_id
  folder_id                = var.folder_id
  service_account_key_file = pathexpand(var.service_account_key_file)
}
module "state" {
  source      = "../modules/storage"
  folder_id   = var.folder_id
  bucket_name = var.state_bucket_name
}
output "state_bucket_name" { value = module.state.bucket_name }
