param deploymentName string

// Hardened public edge routes /webhooks/* → webhook Container App (Appendix I.3).
output publicWebhookBaseUrl string = 'https://webhook.${deploymentName}.example.com'
