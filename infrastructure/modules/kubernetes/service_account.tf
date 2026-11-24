resource "yandex_iam_service_account" "cluster" {
  name      = "${var.name}-master"
  folder_id = var.folder_id
}
resource "yandex_iam_service_account" "nodes" {
  name      = "${var.name}-nodes"
  folder_id = var.folder_id
}
# iam_member добавляет участника, сохраняя другие назначения той же роли.
resource "yandex_resourcemanager_folder_iam_member" "cluster" {
  for_each  = toset(["k8s.clusters.agent", "vpc.publicAdmin", "logging.writer"])
  folder_id = var.folder_id
  role      = each.value
  member    = "serviceAccount:${yandex_iam_service_account.cluster.id}"
}
resource "yandex_resourcemanager_folder_iam_member" "puller" {
  folder_id = var.folder_id
  role      = "container-registry.images.puller"
  member    = "serviceAccount:${yandex_iam_service_account.nodes.id}"
}
resource "yandex_kms_symmetric_key" "secrets" {
  name              = "${var.name}-secrets"
  folder_id         = var.folder_id
  default_algorithm = "AES_256"
  rotation_period   = "8760h"
}
resource "yandex_kms_symmetric_key_iam_binding" "cluster" {
  symmetric_key_id = yandex_kms_symmetric_key.secrets.id
  role             = "kms.keys.encrypterDecrypter"
  members          = ["serviceAccount:${yandex_iam_service_account.cluster.id}"]
}
