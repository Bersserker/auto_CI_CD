variable "folder_id" { type = string }
variable "bucket_name" {
  type = string
  validation {
    condition     = length(var.bucket_name) >= 3 && length(var.bucket_name) <= 63 && can(regex("^[a-z0-9][a-z0-9.-]*[a-z0-9]$", var.bucket_name))
    error_message = "Задайте глобально уникальное S3-имя бакета (3–63 символа)."
  }
}
