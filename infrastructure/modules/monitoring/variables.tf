variable "name" { type = string }
variable "folder_id" { type = string }
variable "cluster_id" { type = string }
variable "retention_period" {
  type    = string
  default = "168h"
}
