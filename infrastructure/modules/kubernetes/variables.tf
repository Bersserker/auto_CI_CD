variable "name" { type = string }
variable "folder_id" { type = string }
variable "network_id" { type = string }
variable "master_version" {
  type = string
}
variable "master_public_ip" {
  type    = bool
  default = false
}
variable "api_allowed_cidrs" {
  type    = list(string)
  default = []
}
variable "subnets" {
  description = "Три непересекающиеся подсети регионального кластера: зона => CIDR."
  type        = map(object({ zone = string, subnet_id = string, cidr = string }))
  validation {
    condition     = length(var.subnets) == 3
    error_message = "Для регионального master нужны три зоны."
  }
}
variable "node_groups" {
  type = map(object({
    zone        = string
    platform_id = optional(string, "standard-v3")
    cores       = optional(number, 2)
    memory      = optional(number, 4)
    gpus        = optional(number, 0)
    disk_size   = optional(number, 64)
    min         = optional(number, 1)
    max         = optional(number, 3)
    initial     = optional(number, 1)
  }))
  validation {
    condition     = alltrue([for n in values(var.node_groups) : n.min >= 0 && n.min <= n.initial && n.initial <= n.max])
    error_message = "Требуется 0 <= min <= initial <= max."
  }
}

variable "pod_cidr" {
  type    = string
  default = "10.112.0.0/16"
}
variable "log_group_id" { type = string }
