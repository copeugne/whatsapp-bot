# Deferred work

- Dependency readiness monitoring: `/health` currently reports process liveness only. The outage showed that this can stay green while 42 credentials fail and WhatsApp disconnects. Add a separately specified readiness endpoint and alerting, with regressions, in a follow-up. It is not necessary to change the public liveness contract to rotate credentials and relink.
- Provisioning TLS handling: `scripts/deploy.sh` regenerates a self-signed certificate, which is not sufficient for Cloudflare Full (strict). Do not rerun provisioning on the working VPS until certificate preservation and trusted origin installation are implemented and tested. Existing recovery used a service restart only.
