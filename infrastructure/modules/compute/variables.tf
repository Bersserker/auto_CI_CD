variable "name" { type = string }
variable "zone" { type = string }
variable "subnet_id" { type = string }
variable "security_group_id" { type = string }
variable "ssh_public_key_path" { type = string }
variable "cores" {
  type    = number
  default = 2
}
variable "memory" {
  type    = number
  default = 2
}
variable "image_family" {
  type    = string
  default = "ubuntu-2204-lts"
}
