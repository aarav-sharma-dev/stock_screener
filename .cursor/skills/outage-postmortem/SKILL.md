---
name: outage-postmortem
description: >-
  Create or update DV Trading OUTAGE postmortems from Slack #alert-outage-*
  channels: read the channel and every thread fully, chase related desk /
  drop-copy / eng channels, draft a Slack Canvas PM, post it, and comment on
  the Jira OUTAGE ticket. Use when the user says create PM, update PM,
  postmortem, or links an alert-outage Slack archive URL.
---

# OUTAGE postmortem

## Goal

Produce an accurate Slack Canvas PM (same structure as prior OUTAGE PMs) from the incident Slack room, without missing follow-ups that live only in threads or related channels.

## Mandatory research (do not skip)

### 1. Incident channel — full history + every thread

1. `slack_read_channel` on the `#alert-outage-*` channel (paginate until no more messages).
2. For **every** parent message that has thread replies (channel output shows `Thread: N replies`), call `slack_read_thread` and incorporate all replies.
3. Also open threads on PagerDuty / Jira bot parents if they have replies.
4. Follow file/canvas IDs already in-channel (existing PM, Devices Down, screenshots) via `slack_read_file` / `slack_read_canvas`.

Do **not** stop at “key” threads. Partial thread reads caused missed RCA (e.g. AlgoManager segment-71 adds, drop-copy deploy).

### 2. Related channels — anything on-topic

Build a related-channel set from:

- Explicit channel mentions / forwards in the OUTAGE room (e.g. `#ztrader_dyn`, desk channels, drop-copy / BOSS, TSE, networking)
- Permalinks in messages (`archives/C…/p…`) — resolve and read that thread
- Desk / product channels named in the ticket title or summary (livestock → Dynasty ztrader; FIX drop copy → drop-copy rooms; etc.)

For each related channel:

1. `slack_read_channel` for the incident date window (and next day for follow-ups).
2. `slack_search_public_and_private` with `in:<#CHANNEL_ID>` + incident keywords (ticket key, product codes, hostnames, segment IDs, “drop copy”, etc.) and `after:YYYY-MM-DD`.
3. Fully read any matching threads (`slack_read_thread`).

If a related channel is private and not in search results, resolve ID via `slack_list_user_channels` (name_prefix) then read directly.

### 3. Before marking an action “Open” or “Done”

Re-check the owning thread / related channel for a later resolve message (e.g. “segments have been deployed”). Prefer the thread’s last word over the OUTAGE room summary.

## Deliverables

1. **Slack Canvas** — structure matching prior PMs: Summary, Desk/PnL, Systems, Teams, How noticed, Timeline, Root cause, What went well / didn’t, Communication, Resolution, How to resolve again, Short/Long term, Action items, Participants.
   - Users: `![](@U…)` · Channels: `![](#C…)` in canvas markdown.
   - New PM: `slack_create_canvas`. Update: `slack_read_canvas` then `slack_update_canvas` with fresh section IDs.
   - **Mandatory AI note** (near the top, under the channel/Jira header line, before Summary):

     `*AI-generated draft from channel / Jira / PD history — review before treating as final.*`

     Per company guidance (#all-it): label AI-assisted content; do not let drafts read as human-verified final RCA. Keep this line on every create **and** update unless the user explicitly asks to remove it after human sign-off.
2. **Post in the OUTAGE channel** — brief “Plz check and update” + **AI-assisted draft** label + title + canvas URL + file id (unless user declines posting). Example:

   `Plz check and update — AI-assisted draft — OUTAGE-NNN … postmortem:`
3. **Jira comment** on the OUTAGE ticket via `addOrEditJiraIssueComment` (cloudId from `getAccessibleAtlassianResources`):
   - Short RCA
   - Canvas link
   - Channel / key thread links
   - Include `AI-assisted draft — please review` (or equivalent) so the ticket mirrors the canvas label
   - Skip only if user says Slack-only

## Update flow

When the user says update PM / consider latest / check related channel X:

1. Re-run mandatory research (OUTAGE channel threads + related channels).
2. Patch the existing canvas; do not invent a second PM unless asked.
3. Comment on Jira with what changed (unless user declines).

## Writing — keep facts, drop chatter

Record **what happened** (state changes, RCA, owners, times, decisions). Do **not** paste conversational acknowledgments or courtesy lines into the PM.

Omit (examples):
- “Noah: no worries thank you joseph.”
- “Thanks”, “ty”, “sounds good”, “:+1:”, “Appreciate it”, “Will do” with no new fact
- Reaction-only or emoji-only outcomes phrased as narrative

Prefer: “Joseph deployed the new drop-copy segments (~17:52); Noah ACK’d.” or simply “Joseph deployed … (~17:52).” — not quoting the thank-you.

## Anti-patterns

- Summarizing from channel top-level only
- Reading one reply in a thread and ignoring the rest
- Leaving drop-copy / desk / gateway follow-ups “Open” without checking the linked thread
- Skipping Jira comment on create/update
- Quoting thanks / “no worries” / small-talk in Summary, Timeline, or Desk impact
- Posting a PM canvas / channel message / Jira comment **without** the AI-assisted label
