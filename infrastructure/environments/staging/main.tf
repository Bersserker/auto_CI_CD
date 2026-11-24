module "network" {
  source            = "../../modules/network"
  name              = "mlops-staging"
  zone              = var.zone
  subnet_cidr       = "10.2.0.0/24"
  ssh_allowed_cidrs = var.ssh_allowed_cidrs
  additional_subnets = {
    ru-central1-a = { zone = "ru-central1-a", cidr = "10.20.0.0/24" }
    ru-central1-b = { zone = "ru-central1-b", cidr = "10.20.1.0/24" }
    ru-central1-d = { zone = "ru-central1-d", cidr = "10.20.2.0/24" }
  }
}
module "compute" {
  count               = var.enable_compute ? 1 : 0
  source              = "../../modules/compute"
  name                = "mlops-staging"
  zone                = var.zone
  subnet_id           = module.network.subnet_id
  security_group_id   = module.network.security_group_id
  ssh_public_key_path = var.ssh_public_key_path
  cores               = var.cores
  memory              = var.memory
}
module "storage" {
  source      = "../../modules/storage"
  folder_id   = var.folder_id
  bucket_name = var.artifacts_bucket_name
}
module "monitoring" {
  count            = var.enable_kubernetes ? 1 : 0
  source           = "../../modules/monitoring"
  name             = "mlops-staging"
  folder_id        = var.folder_id
  cluster_id       = module.kubernetes[0].cluster_id
  retention_period = "168h"
}
module "kubernetes" {
  count             = var.enable_kubernetes ? 1 : 0
  source            = "../../modules/kubernetes"
  name              = "mlops-staging"
  folder_id         = var.folder_id
  network_id        = module.network.network_id
  master_version    = var.kubernetes_version
  master_public_ip  = var.kubernetes_public_ip
  api_allowed_cidrs = var.kubernetes_api_allowed_cidrs
  log_group_id      = module.monitoring[0].log_group_id
  subnets           = module.network.kubernetes_subnets
  node_groups = merge({
    cpu = { zone = "ru-central1-a", min = 1, max = 3, initial = 1 }
    }, var.enable_gpu ? {
    gpu = {
      zone        = "ru-central1-a"
      platform_id = "gpu-standard-v2"
      cores       = 8
      memory      = 48
      gpus        = 1
      min         = 1
      initial     = 1
      max         = 2
    }
  } : {})
}
