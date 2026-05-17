# French Laundry reservation watcher

A Lambda that watches Tock for French Laundry availability and sends an
SMS — and, once your target reservation date is bookable, places an
outbound voice call — via AWS End User Messaging.

It runs on two EventBridge schedules:

- **Hourly (edge mode)** — `cron(1 * * * ? *)` America/Los_Angeles.
  Checks only `today + DAYS_AHEAD`, the date newly entering the rolling
  window at midnight PT. Lets you observe the release fill-curve in
  CloudWatch.
- **Daily (rolling mode)** — `cron(30 8 * * ? *)` America/Los_Angeles.
  Sweeps `today+1 .. today+DAYS_AHEAD-1` once a day to catch
  cancellation drops.

## Notification policy

| Phase                                            | SMS | Voice call |
| ------------------------------------------------ | --- | ---------- |
| Before `TARGET_DATE` enters the rolling window   | yes | no         |
| Once `TARGET_DATE` is within `DAYS_AHEAD` days   | yes | yes        |

Voice calls activate on the day `today >= TARGET_DATE - DAYS_AHEAD`.
With `DAYS_AHEAD=60` and `TARGET_DATE=2026-10-01`, calls begin on
**2026-08-02**. Before that you'll get SMS notifications only — handy
for learning the release pattern without a 12:01 AM phone call.

> **Important caveats**
>
> - Tock's terms prohibit automated booking. This tool only **notifies**;
>   you complete the reservation yourself.
> - Tock has no public API. The handler hits the same endpoint the
>   in-page widget uses, with browser-like headers. If Tock changes the
>   endpoint or response shape, update `TOCK_SEARCH_URL` / `extract_slots`
>   in `lambda/handler.py`. CloudWatch logs the raw response on parse
>   failures.
> - Tock fronts requests with Cloudflare. AWS Lambda egress IPs may be
>   challenged. If you see persistent 403s, route the Lambda through a
>   NAT'd VPC with a stable IP and/or add a residential proxy.

## Architecture

```
EventBridge Scheduler
  ├─ hourly  (cron 1 * * * ? *,  America/Los_Angeles) ─┐  input={"mode":"edge"}
  └─ daily   (cron 30 8 * * ? *, America/Los_Angeles) ─┤  input={"mode":"rolling"}
                                                       ▼
                                              Lambda (handler.py)
                                                       │
                                                       ▼
                                              Tock search endpoint
                                                       │
                       ┌───────────────────────────────┴─────────────────────────────┐
                       ▼                                                             ▼
       AWS End User Messaging Voice                                AWS End User Messaging SMS
       (SendVoiceMessage, gated by TARGET_DATE)                    (SendTextMessage, always)
```

## Layout

```
french-laundry-agent/
├── lambda/handler.py     # Lambda code
└── infra/main.tf         # Terraform: Lambda + Scheduler + IAM + logs
```

## Deploy

Prerequisites:

- Terraform ≥ 1.5, AWS credentials with permissions for Lambda, IAM,
  CloudWatch Logs, EventBridge Scheduler.
- An AWS End User Messaging origination number in your account.
- Your destination phone verified in End User Messaging (or the account
  graduated out of sandbox).

```bash
cd french-laundry-agent/infra

terraform init
terraform apply \
  -var 'origination_number=+15551234567' \
  -var 'destination_number=+15557654321' \
  -var 'target_date=2026-10-01' \
  -var 'party_size=2' \
  -var 'days_ahead=60'
```

To test the Lambda once without waiting for the next schedule:

```bash
# Edge mode (newly-opening date only)
aws lambda invoke --function-name french-laundry-watcher \
  --payload '{"mode":"edge"}' --cli-binary-format raw-in-base64-out /tmp/out.json && cat /tmp/out.json

# Rolling mode (already-open dates)
aws lambda invoke --function-name french-laundry-watcher \
  --payload '{"mode":"rolling"}' --cli-binary-format raw-in-base64-out /tmp/out.json && cat /tmp/out.json
```

## Tuning

Environment variables on the Lambda (set via Terraform variables):

| Variable             | Default  | Purpose                                       |
| -------------------- | -------- | --------------------------------------------- |
| `TOCK_BUSINESS`      | `tfl`    | Tock business slug (`tfl` = The French Laundry) |
| `PARTY_SIZE`         | `2`      | Party size to search                          |
| `DAYS_AHEAD`         | `60`     | Days to scan from today                       |
| `ORIGINATION_NUMBER` | required | AWS-provisioned phone number, E.164           |
| `DESTINATION_NUMBER` | required | Your phone, E.164                             |
| `VOICE_ID`           | `Joanna` | Polly voice for the call                      |
| `TARGET_DATE`        | required | `YYYY-MM-DD`. Voice calls start once this date is bookable. |

## Observing the release pattern

Every scan writes a structured JSON log line:

```json
{"event":"scan","mode":"edge","date":"2026-08-02","slot_count":7,"target_date":"2026-10-01"}
```

CloudWatch Logs Insights query to plot fill-rate of the edge date:

```
fields @timestamp, date, slot_count
| filter event = "scan" and mode = "edge"
| sort @timestamp asc
```

## Verifying the Tock endpoint

The first deploy is a good moment to confirm the search request:

1. Open https://www.exploretock.com/tfl in a browser, DevTools → Network.
2. Pick a date and party size; watch for an XHR to a path containing
   `consumer` or `search`.
3. If it differs from `TOCK_SEARCH_URL`, update the constant and
   `extract_slots` to match the response, then `terraform apply` again.
