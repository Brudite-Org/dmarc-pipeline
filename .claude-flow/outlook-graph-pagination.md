---
name: outlook-graph-pagination
description: Outlook sync must follow @odata.nextLink; single-page $top fetch silently misses reports
metadata:
  type: project
---

Fact: `services/outlook_sync.py::sync_account_emails` fetched only the first Graph page (`$top=50`, no `@odata.nextLink` follow). On a busy mailbox the newest 50 messages are rarely DMARC reports, so syncs scanned "many emails" yet found nothing — and `processed_emails` marking made the miss permanent.

Fix (2026-09-23): paginated loop following `@odata.nextLink` up to `MAX_PAGES` (env `OUTLOOK_SYNC_MAX_PAGES`, default 10 → ≤500 msgs/sync). Early-stop: steady-state (non-backfill) stops after one fully-processed page since results are newest-first; backfill always walks. Fresh-filtering via `is_processed` before fetching attachments so already-seen pages cost zero extra API calls.

**Why:** Render prod (`dmarc@skillbrew.com`) showed "Last sync" succeeding with 0 new reports despite a full inbox; Graph default page size is 10 and `$top` caps at 1000 — single-page fetch is never a complete scan (verified against Microsoft Graph list-messages docs).

**How to apply:** any new Graph/Gmail list call must follow paging (`@odata.nextLink` / Gmail `nextPageToken`) — DONE for both: `services/gmail_sync.py` got the same `nextPageToken` loop + `GMAIL_SYNC_MAX_PAGES` on 2026-09-23, with 6 regression tests in `tests/test_gmail_pagination.py`.

Related: [[outlook-token-refresh]] (callback stores no `expires_at`, forcing refresh-every-call — next suspect if zero-scan persists after this fix).
