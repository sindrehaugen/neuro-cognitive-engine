variable "deployment_name" {
  type = string
}

variable "worker_log_group" {
  type        = string
  description = "ECS worker log group name for dashboard linking"
}

resource "aws_cloudwatch_dashboard" "trimcp" {
  dashboard_name = "trimcp-${replace(var.deployment_name, "_", "-")}-ops"
  dashboard_body = jsonencode({
    widgets = [
      {
        type   = "log"
        x      = 0
        y      = 0
        width  = 24
        height = 6
        properties = {
          query   = "SOURCE '${var.worker_log_group}' | fields @timestamp, @message | sort @timestamp desc | limit 50"
          region  = data.aws_region.current.name
          title   = "RQ worker (recent logs)"
          stacked = false
        }
      }
    ]
  })
}

data "aws_region" "current" {}

output "dashboard_name" {
  value = aws_cloudwatch_dashboard.trimcp.dashboard_name
}
