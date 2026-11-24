output "log_group_id" { value = yandex_logging_group.cluster.id }
output "dashboard_id" { value = yandex_monitoring_dashboard.cluster.id }
output "dashboard_url" {
  value = "https://monitoring.yandex.cloud/folders/${var.folder_id}/dashboards/${yandex_monitoring_dashboard.cluster.id}"
}
