resource "yandex_logging_group" "cluster" {
  name             = "${var.name}-kubernetes"
  folder_id        = var.folder_id
  retention_period = var.retention_period
}
resource "yandex_monitoring_dashboard" "cluster" {
  name      = "${var.name}-infrastructure"
  title     = "${var.name}: Kubernetes infrastructure"
  folder_id = var.folder_id
  dynamic "widgets" {
    for_each = {
      memory = { title = "Node memory (bytes)", metric = "node.memory.used_bytes", x = 0, y = 0 }
      disk   = { title = "Node filesystem used (bytes)", metric = "node.ephemeral_storage.used_bytes", x = 18, y = 0 }
      cpu    = { title = "Master CPU utilization (%)", metric = "master.cpu.utilization_percent", x = 0, y = 8 }
    }
    content {
      chart {
        title          = widgets.value.title
        display_legend = true
        queries {
          target {
            query = <<-QUERY
              {folderId="${var.folder_id}", service="managed-kubernetes", cluster_id="${var.cluster_id}", name="${widgets.value.metric}"}
            QUERY
          }
        }
      }
      position {
        x = widgets.value.x
        y = widgets.value.y
        w = 18
        h = 8
      }
    }
  }
}
