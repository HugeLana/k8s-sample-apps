# French Laundry reservation watcher

Two Lambdas:

- **Watcher** runs on schedule, queries Tock for availability, records
  every scan in DynamoDB, and (once your target reservation date is
  bookable) sends an SMS and a voice call via AWS End User Messaging.
- **Dashboard** is invoked via a Lambda Function URL and renders an HTML
  page with a Chart.js plot of the fill curve plus a recent-scans table.

Watcher schedules:

- **Hourly (edge mode)** — `cron(1 * * * ? *)` America/Los_Angeles.
  Checks only `today + DAYS_AHEAD`, the date entering the rolling
  window at midnight PT. The hourly cadence captures the fill curve of
  the fresh release.
- **Daily (rolling mode)** — `cron(30 8 * * ? *)` America/Los_Angeles.
  Sweeps `today+1 .. today+DAYS_AHEAD-1` once a day to catch
  cancellation drops.

## Notification policy

Behavior depends on whether `TARGET_DATE` is yet inside the rolling
`DAYS_AHEAD` window:

| Phase                                                                  | Dashboard | SMS | Voice |
| ---------------------------------------------------------------------- | --------- | --- | ----- |
| **Phase 1** — `today <  TARGET_DATE - DAYS_AHEAD` (silent collection)  | yes       | no  | no    |
| **Phase 2** — `today >= TARGET_DATE - DAYS_AHEAD` (target now bookable) | yes       | yes | yes   |

With `DAYS_AHEAD=60` and `TARGET_DATE=2026-10-01`, alerts switch on
**2026-08-02**. Until then the dashboard is the only signal — quiet
data collection so you can study the release pattern. Once Phase 2
starts, every hourly scan that finds availability fires both SMS and
a voice call.

> **Caveats**
>
> - Tock's terms prohibit automated booking. This tool only notifies;
>   you complete the reservation yourself.
> - Tock has no public API. The handler hits the same endpoint the
>   in-page widget uses, with browser-like headers. If Tock changes
>   the shape, update `TOCK_SEARCH_URL` / `extract_slots` in
>   `lambda/handler.py`. CloudWatch logs the raw response on parse
>   failures.
> - Tock fronts requests with Cloudflare. AWS Lambda egress IPs may
>   be challenged. If you see persistent 403s, route the Lambda
>   through a NAT'd VPC with a stable IP and/or add a residential
>   proxy.

## Architecture

```
EventBridge Scheduler
  ├─ hourly  (cron 1 * * * ? *,  America/Los_Angeles)  input={"mode":"edge"}
  └─ daily   (cron 30 8 * * ? *, America/Los_Angeles)  input={"mode":"rolling"}
                                │
                                ▼
                    Watcher Lambda (handler.py)  ──►  Tock search endpoint
                                │
                                ├─►  DynamoDB scans table  (every scan)
                                │
                                └─►  (Phase 2 only) AWS End User Messaging
                                        ├─ SendVoiceMessage
                                        └─ SendTextMessage

                    Dashboard Lambda (dashboard.py)
                                ▲
                                │ Function URL (HTML)
                                │
                          Your browser
```

## Layout

```
french-laundry-agent/
├── lambda/handler.py        # watcher
├── dashboard/dashboard.py   # dashboard
└── infra/main.tf            # Terraform for both Lambdas + schedules + DDB
```

## Deploy

Prerequisites:

- Terraform ≥ 1.5, AWS credentials with permissions for Lambda, IAM,
  CloudWatch Logs, EventBridge Scheduler, DynamoDB.
- An AWS End User Messaging origination number in your account.
- Your destination phone verified in End User Messaging (or the
  account graduated out of sandbox).

```bash
cd french-laundry-agent/infra

terraform init
terraform apply \
  -var 'origination_number=+15551234567' \
  -var 'destination_number=+15557654321' \
  -var 'target_date=2026-10-01' \
  -var 'party_size=2' \
  -var 'days_ahead=60' \
  -var "dashboard_key=$(openssl rand -hex 16)"
```

The Function URL is printed as `dashboard_url`. Visit it with
`?key=<value>` appended (or set `dashboard_key=""` to make it
unauthenticated).

To trigger a scan immediately:

```bash
# Edge mode (newly-opening date only)
aws lambda invoke --function-name french-laundry-watcher \
  --payload '{"mode":"edge"}' --cli-binary-format raw-in-base64-out /tmp/out.json && cat /tmp/out.json

# Rolling mode (already-open dates)
aws lambda invoke --function-name french-laundry-watcher \
  --payload '{"mode":"rolling"}' --cli-binary-format raw-in-base64-out /tmp/out.json && cat /tmp/out.json
```

## Tuning

Watcher environment variables (set via Terraform vars of the same name):

| Variable              | Default  | Purpose                                       |
| --------------------- | -------- | --------------------------------------------- |
| `TOCK_BUSINESS`       | `tfl`    | Tock business slug                            |
| `PARTY_SIZE`          | `2`      | Party size to search                          |
| `DAYS_AHEAD`          | `60`     | Days to scan from today                       |
| `ORIGINATION_NUMBER`  | required | AWS-provisioned phone number, E.164           |
| `DESTINATION_NUMBER`  | required | Your phone, E.164                             |
| `VOICE_ID`            | `Joanna` | Polly voice for the call                      |
| `TARGET_DATE`         | required | `YYYY-MM-DD`. Alerts activate once this date is in the window. |
| `SCANS_TABLE`         | required | DynamoDB table for scan history (Terraform sets this). |
| `SCAN_RETENTION_DAYS` | `90`     | DDB TTL for scan rows.                        |

Dashboard variables:

| Variable        | Default  | Purpose                                       |
| --------------- | -------- | --------------------------------------------- |
| `DASHBOARD_KEY` | `""`     | If set, the URL requires `?key=<value>`.      |

## Dashboard

Open the `dashboard_url` (with `?key=...` if you set one). You'll see:

- A banner showing **Phase 1** (silent) or **Phase 2** (alerting),
  with the countdown to alert activation.
- A line chart of slot counts over time for the edge date (today+60)
  and the daily rolling sum.
- A table of the most recent 80 scans.
- Window picker: 1d / 7d / 14d / 30d.

The scans table itself is queryable directly if you want raw data:

```bash
aws dynamodb query \
  --table-name french-laundry-scans \
  --key-condition-expression "pk = :p" \
  --expression-attribute-values '{":p":{"S":"scan"}}'
```

## Verifying the Tock endpoint

The first deploy is a good moment to confirm the search request:

1. Open https://www.exploretock.com/tfl in a browser, DevTools → Network.
2. Pick a date and party size; watch for an XHR to a path containing
   `consumer` or `search`.
3. If it differs from `TOCK_SEARCH_URL`, update the constant and
   `extract_slots` to match the response, then `terraform apply` again.
