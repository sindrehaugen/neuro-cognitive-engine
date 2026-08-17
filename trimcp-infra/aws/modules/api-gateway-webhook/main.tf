# Stub webhook ingress (I.6). Replace the Lambda with trimcp/webhook_receiver (Mangum/ASGI)
# before exposing this API publicly. Until then the handler returns 503 by design.

variable "deployment_name" {
  type = string
}

data "archive_file" "lambda_zip" {
  type        = "zip"
  output_path = "${path.module}/webhook_stub.zip"
  source {
    content  = <<-PY
import json
def handler(event, context):
    return {
        "statusCode": 503,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps({
            "error": "webhook handler not deployed",
            "detail": "Replace this stub with trimcp/webhook_receiver before production use",
        }),
    }
PY
    filename = "index.py"
  }
}

resource "aws_iam_role" "lambda" {
  name = "trimcp-${substr(var.deployment_name, 0, 16)}-webhook-lambda"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Action    = "sts:AssumeRole"
      Effect    = "Allow"
      Principal = { Service = "lambda.amazonaws.com" }
    }]
  })
}

resource "aws_iam_role_policy_attachment" "basic" {
  role       = aws_iam_role.lambda.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"
}

resource "aws_lambda_function" "webhook" {
  function_name = "trimcp-${var.deployment_name}-webhook"
  role          = aws_iam_role.lambda.arn
  handler       = "index.handler"
  runtime       = "python3.12"
  filename      = data.archive_file.lambda_zip.output_path
  source_code_hash = data.archive_file.lambda_zip.output_base64sha256

  tags = { Name = "trimcp-webhook-stub" }
}

resource "aws_apigatewayv2_api" "http" {
  name          = "trimcp-${var.deployment_name}-webhooks"
  protocol_type = "HTTP"
}

resource "aws_apigatewayv2_integration" "lambda" {
  api_id                 = aws_apigatewayv2_api.http.id
  integration_type       = "AWS_PROXY"
  integration_uri        = aws_lambda_function.webhook.invoke_arn
  integration_method     = "POST"
  payload_format_version = "2.0"
}

resource "aws_apigatewayv2_route" "webhooks" {
  api_id    = aws_apigatewayv2_api.http.id
  route_key = "ANY /webhooks/{proxy+}"
  target    = "integrations/${aws_apigatewayv2_integration.lambda.id}"
}

resource "aws_apigatewayv2_route" "health" {
  api_id    = aws_apigatewayv2_api.http.id
  route_key = "GET /health"
  target    = "integrations/${aws_apigatewayv2_integration.lambda.id}"
}

resource "aws_apigatewayv2_stage" "default" {
  api_id      = aws_apigatewayv2_api.http.id
  name        = "$default"
  auto_deploy = true
}

resource "aws_lambda_permission" "apigw" {
  statement_id  = "AllowExecutionFromAPIGateway"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.webhook.function_name
  principal     = "apigateway.amazonaws.com"
  source_arn    = "${aws_apigatewayv2_api.http.execution_arn}/*/*"
}

output "invoke_url" {
  description = "Public HTTPS base URL for webhooks (I.6 — sole managed inbound HTTP surface)"
  value       = aws_apigatewayv2_api.http.api_endpoint
}

output "lambda_arn" {
  value = aws_lambda_function.webhook.arn
}
