# TradingView/Pine lifecycle webhook receiver

The receiver accepts Pine lifecycle evidence and passes it to
`CandidateLifecycleTracker`. It does not import a broker client, place orders,
or change scanner and Pine selection rules. The only public application routes
are `POST /webhooks/tradingview/pine` and `GET /health`.

The implementation uses Python's standard-library `ThreadingHTTPServer`. The
repository has no existing application web framework, so this keeps the
service isolated and adds no production dependency. Bind it to loopback and
put a TLS reverse proxy in front of it; do not expose plain HTTP to the
internet.

## Configuration and startup

Set a long random secret in the service environment. Values in the process
environment take precedence over repository dotenv files.

```bash
export SIGNALMONITOR_PINE_WEBHOOK_SECRET='replace-with-a-long-random-value'
.venv/bin/python scripts/pine_webhook_server.py \
  --output-dir /path/to/pkscreener-user-data
```

The default listener is `127.0.0.1:8787`. All available settings and their
defaults are listed in `.env.example`. The lifecycle output directory must be
the same one used by `SignalMonitorRunner` so candidate matching sees the same
SQLite database.

Example systemd service:

```ini
[Unit]
Description=PKScreener Pine lifecycle webhook
After=network.target

[Service]
Type=simple
User=pkscreener
WorkingDirectory=/opt/tr-assistant
EnvironmentFile=/etc/pkscreener/pine-webhook.env
ExecStart=/opt/tr-assistant/.venv/bin/python scripts/pine_webhook_server.py --output-dir /var/lib/pkscreener
Restart=on-failure
NoNewPrivileges=true
PrivateTmp=true

[Install]
WantedBy=multi-user.target
```

Configure the reverse proxy to preserve the `Authorization` header, enforce
HTTPS, and proxy only the two documented routes. `X-Forwarded-For` is ignored
unless the immediate peer belongs to
`SIGNALMONITOR_PINE_WEBHOOK_TRUSTED_PROXY_IPS`. Source addresses never replace
shared-secret authentication.

## TradingView request

Preferred request:

```http
POST /webhooks/tradingview/pine HTTP/1.1
Authorization: Bearer replace-with-the-secret
Content-Type: application/json

{"event_id":"TV-RELIANCE-20260927-093600-ENTRY","event":"ENTRY","symbol":"NSE:RELIANCE","exchange":"NSE","timestamp":"2026-09-27T09:36:00+05:30","assumed_entry":152.50,"qty":500,"initial_stop":149.80,"one_r":155.20,"target":157.90}
```

Supported event names are `BUY_READY`, `ENTRY`, `BUY` (normalized to `ENTRY`),
`LOW_LIQUIDITY_SKIP`, `STOP_UPDATE`, and `EXIT`. Symbols such as `RELIANCE`,
`NSE:RELIANCE`, and `NSE:RELIANCE-EQ` use the existing lifecycle normalizer.
Supplied timestamps must be ISO-8601 with an offset. If omitted, the authenticated
server receive time is used. Old and future timestamps are bounded by the two
configured tolerance settings.

TradingView configurations that cannot set an authorization header may set
`SIGNALMONITOR_PINE_WEBHOOK_ALLOW_PATH_SECRET=1` and post to
`/webhooks/tradingview/pine/<secret>`. Header authentication is safer: path
credentials can leak through proxy, browser, or monitoring logs. The receiver
suppresses its own raw-path access log, but every upstream component must do the
same when path authentication is enabled.

## Responses, retries, and storage

A new event returns `201`, while a retry returns `200` with
`"status":"duplicate"`. If `event_id` is present it is the idempotency identity;
otherwise a fingerprint is derived from normalized lifecycle fields. The unique
SQLite index makes this atomic across concurrent requests. A retry does not
reapply entry, stop, or exit state.

Valid unmatched or out-of-order events are retained. In particular, an exit or
stop received before a matched entry is marked `out_of_order` and cannot invent
or modify a trade. Raw JSON is retained for audit, with secret-like keys
redacted. Authorization headers are neither stored nor logged.

Typical errors are `400` for invalid JSON/schema/timestamps, `401` for failed
authentication, `413` for an oversized request, `415` for non-JSON content, and
`429` for rate limiting. Responses never expose database paths or stack traces.

## Secret rotation

Generate a new random value, replace `SIGNALMONITOR_PINE_WEBHOOK_SECRET` in the
protected service environment and in the TradingView delivery configuration,
then restart the service. Coordinate the two updates closely because the
receiver intentionally accepts only one current secret. Confirm `GET /health`
after restart and send a harmless uniquely identified `BUY_READY` test event.
