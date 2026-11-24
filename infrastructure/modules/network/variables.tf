variable "name" { type = string }
variable "zone" { type = string }
variable "subnet_cidr" { type = string }
variable "ssh_allowed_cidrs" {
  type    = list(string)
  default = []
}

variable "additional_subnets" {
  type    = map(object({ zone = string, cidr = string }))
  default = {}
}
