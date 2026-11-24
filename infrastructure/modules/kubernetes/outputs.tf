output "cluster_id" { value = yandex_kubernetes_cluster.regional.id }
output "internal_endpoint" { value = yandex_kubernetes_cluster.regional.master[0].internal_v4_endpoint }
output "external_endpoint" { value = yandex_kubernetes_cluster.regional.master[0].external_v4_endpoint }
output "node_group_ids" { value = { for k, n in yandex_kubernetes_node_group.workers : k => n.id } }
