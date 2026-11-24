output "network_id" { value = yandex_vpc_network.test-vpc.id }
output "subnet_id" { value = yandex_vpc_subnet.test-subnet.id }
output "security_group_id" { value = yandex_vpc_security_group.test-sg.id }

output "kubernetes_subnets" {
  value = { for k, subnet in yandex_vpc_subnet.additional : k => {
    zone      = subnet.zone
    subnet_id = subnet.id
    cidr      = subnet.v4_cidr_blocks[0]
  } }
}
