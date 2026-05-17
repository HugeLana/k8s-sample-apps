# French Laundry reservation watcher

A Lambda that runs at 00:00 America/Los_Angeles every night, queries Tock
for French Laundry availability across a rolling window, and — if any
slot is open — places an outbound voice call **and** sends an SMS via
AWS End User Messaging.

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
EventBridge Scheduler (cron, America/Los_Angeles)
        │
        ▼
Lambda (handler.py)  ──►  Tock search endpoint
        │
        ├─► AWS End User Messaging Voice (SendVoiceMessage)
        └─► AWS End User Messaging SMS   (SendTextMessage)
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
  -var 'party_size=2' \
  -var 'days_ahead=60'
```

To test the Lambda once without waiting for midnight:

```bash
aws lambda invoke \
  --function-name french-laundry-watcher \
  --payload '{}' \
  /tmp/out.json && cat /tmp/out.json
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

## Verifying the Tock endpoint

The first deploy is a good moment to confirm the search request:

1. Open https://www.exploretock.com/tfl in a browser, DevTools → Network.
2. Pick a date and party size; watch for an XHR to a path containing
   `consumer` or `search`.
3. If it differs from `TOCK_SEARCH_URL`, update the constant and
   `extract_slots` to match the response, then `terraform apply` again.
