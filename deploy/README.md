# Deployment notes

The production layout is `/opt/whatsapp-bot`, with the bot running as `whatsapp-bot` under systemd. Docker hosts Evolution, PostgreSQL and Redis. nginx handles the HTTPS reverse proxy.

## Existing production host

Use `ssh oci-main`. Keep secrets in `/opt/whatsapp-bot/.env` and `/opt/whatsapp-bot/evolution.env`, mode 0600, owned by the service user. Never copy local development environment files over production.

To rotate 42 credentials, securely edit `BOT_FT_CLIENT_ID` and `BOT_FT_CLIENT_SECRET`, then:

```bash
sudo systemctl restart whatsapp-bot
sudo systemctl is-active whatsapp-bot
sudo journalctl -u whatsapp-bot --since '5 minutes ago' --no-pager
```

Confirm successful token acquisition and event/offer HTTP 200 responses. Do not paste tokens or unredacted logs into public issues. Ensure the registered OAuth callback matches `BOT_FT_REDIRECT_URI`.

For WhatsApp, use Evolution's authenticated `/instance/connectionState/default`. If disconnected, request `/instance/connect/default` and scan the returned QR in WhatsApp Linked devices. Treat QR codes as credentials. Do not delete the instance or Docker volumes to reconnect. Successful recovery requires state `open` and a message received by the intended group.

## Provisioning

`scripts/push-and-deploy.sh` uses SSH alias `oci-main` by default (`VPS` overrides it) and remote passwordless sudo for rsync. Review `scripts/deploy.sh` before provisioning. Supply the host address via `VPS_IP`, and review domain-specific nginx configuration. Configure the destination group yourself; provisioning preserves it.

The installer is host-specific, not a generic cloud installer. It changes firewall and TLS configuration and restarts dependencies. Its generated self-signed TLS certificate is not a Cloudflare Origin CA certificate. For Cloudflare Full (strict), install an actual trusted origin certificate and private key securely before exposing the site. Never commit either file. Do not rerun provisioning on an existing host without reviewing certificate handling.

## Backups and monitoring

Back up encrypted `data/`, both environment files, the encryption key and Docker database/session volumes securely. Keep them out of Git. Losing the encryption key makes stored OAuth tokens unreadable.

The public `/health` endpoint reports liveness only. Monitor scraper errors, 42 `invalid_client` failures and WhatsApp connection state separately. See [recovery evidence](../reports/recovery.md) for the bounded 2026-10-17 incident investigation.
