variable "cloud_id" { type = string }
variable "folder_id" { type = string }
variable "service_account_key_file" { type = string }
variable "artifacts_bucket_name" { type = string }
variable "kubernetes_version" {
  description = "Явно выбранная доступная версия Kubernetes; используется для master и node groups."
  type        = string
  nullable    = false
  validation {
    condition     = can(regex("^[0-9]+\\.[0-9]+$", var.kubernetes_version))
    error_message = "Укажите версию вида 1.XX из доступных в Yandex Cloud."
  }
}
variable "enable_kubernetes" {
  type    = bool
  default = true
}
variable "enable_compute" {
  description = "Дополнительная учебная VM; для этапа 2 не требуется."
  type        = bool
  default     = false
}
variable "ssh_public_key_path" {
  type    = string
  default = "~/.ssh/id_ed25519.pub"
}
variable "zone" {
  type    = string
  default = "ru-central1-a"
}
variable "ssh_allowed_cidrs" {
  type    = list(string)
  default = []
}
variable "kubernetes_public_ip" {
  type    = bool
  default = false
}
variable "kubernetes_api_allowed_cidrs" {
  type    = list(string)
  default = []
}
variable "enable_gpu" {
  description = "Для полного этапа 2 оставьте true; требуется GPU-квота."
  type        = bool
  default     = true
}
variable "cores" {
  type    = number
  default = 2
}
variable "memory" {
  type    = number
  default = 2
}
