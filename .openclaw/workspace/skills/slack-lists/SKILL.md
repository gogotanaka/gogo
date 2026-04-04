---
name: slack-lists
description: Fetch and display Slack Lists records for the aisaac workspace via CDP (Chrome DevTools Protocol). Use when the user asks to retrieve, summarize, or check Slack Lists data, task records, or list items from the aisaac Slack workspace.
---

# slack-lists

Fetch Slack List records from the aisaac workspace using Chrome DevTools Protocol (CDP) to extract auth tokens from a running Slack desktop/web session.

## Prerequisites

- Slack must be open in Chrome (or Slack desktop with remote debugging enabled on port 9222)
- `websocket-client` Python package: `pip install websocket-client`

## Usage

```bash
python3 scripts/fetch_slack_lists.py [LIST_ID]
```

- `LIST_ID` is optional; defaults to `F07BYFCPUPK` (the main aisaac list)
- Output is JSON array printed to stdout

## Output Fields

| Field | Description |
|-------|-------------|
| `name` | Record title (up to 200 chars) |
| `status` | `"active"`, `"done"`, or `"知見"` |
| `date` | Date field value |
| `next_date` | Next action date |
| `created` | Record creation date (YYYY-MM-DD) |
| `list_link` | Direct link to record in Slack Lists |
| `message_link` | Link to the source Slack message/thread |
| `latest_reply` | Most recent reply text in the thread (active records only, up to 200 chars) |

## Key Constants (aisaac workspace)

- `TEAM_ID`: `T08KK9UCW`
- `TEAM_DOMAIN`: `aisaac`
- Default `LIST_ID`: `F07BYFCPUPK`
- Status column IDs: `Col09BFAWGRT2`, `Col09F7VDC03G`
- Done option values: `OptM26K87YJ`, `Opt3YFPZR2L`
- 知見 option value: `Opt7I23CQYZ`

## How It Works

1. Connects to Chrome via CDP at `localhost:9222`
2. Extracts `xoxd` cookie and `xoxc` token from the active Slack page
3. Calls `lists.records.list` API with the list ID
4. For active records, fetches latest thread replies concurrently (10 workers)
5. Returns structured JSON

## Notes

- CDP connection requires Chrome launched with `--remote-debugging-port=9222`, or use the OpenClaw browser tool (profile=`chrome`) which already has CDP enabled
- If Chrome DevTools port differs, update `localhost:9222` in `get_tokens()`
