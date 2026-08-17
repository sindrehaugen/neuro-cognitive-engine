# Post-deploy checklist (Cloud mode)

1. **Secrets:** Confirm rotation policies on Postgres / DocumentDB / Redis secrets (provider-native).
2. **pgvector:** Connect from a bastion or VPN-attached runner and run `CREATE EXTENSION IF NOT EXISTS vector;` on the application database if not already applied by automation.
3. **Webhook URL:** Verify TLS on `webhook_dns_name`; confirm WAF / Cloud Armor / Front Door rules.
4. **Egress:** Confirm workers can reach Microsoft Graph / Google / Dropbox APIs via NAT/NAT Gateway/Cloud NAT.
5. **Client .env:** Run `scripts/render-env.sh` and distribute the file through your secure channel — never commit.
6. **Least privilege:** Audit IAM / managed identities: workers need read secrets + blob prefix only.
