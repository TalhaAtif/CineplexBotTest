# Doomsday watch

> [!NOTE]
> **Vibe-coded.** I made this with AI for one reason: to help me get Avengers: Doomsday tickets. I didn't write
> the code for this particular app myself.

Pings your phone the moment **Avengers: Doomsday** showtimes go on sale at **Cineplex Junxion Erin Mills**
(Mississauga), and gives you a heads-up whenever the theatre posts its next batch of showtimes.

## What it does

- **🎟️ On-sale alert (urgent).** GitHub Actions runs `watch.py` every ~5 minutes. It asks Cineplex's own showtimes
  API (the one cineplex.com uses) about Junxion Erin Mills (theatre 7313) for Dec 16–20. When bookable Doomsday
  showtimes appear, you get an urgent push listing every showtime and format (UltraAVX, D-BOX…) with a button
  straight into seat selection. After that it only pings you about newly added showtimes.
- **📅 New-showtimes pings.** Junxion only posts about 8 days ahead. Each time it posts its next batch, you get a
  normal ping saying which dates were added, when the bot spotted them, and when earlier batches dropped, so you
  learn the pattern before Doomsday week. A lone early presale (one big premiere weeks out) doesn't count.
- **⚠️ Self-monitoring.** If the bot can't read Cineplex (API change, blocked network), it tells you instead of
  failing silently, and tells you again when it recovers. If Cineplex rotates its public API key, the bot re-reads
  the new one from cineplex.com on its own.

Alerts go to [ntfy](https://ntfy.sh) (free phone push, no account needed) and/or a Discord webhook.

## Setup (~5 min)

1. **Phone:** install **ntfy** (App Store / Google Play), tap **+**, and subscribe to a topic name only you know,
   e.g. `doomsday-` plus a few random letters. Topic names on ntfy.sh are public, so make it unguessable and never
   commit it.
2. **Test it (optional, needs Python 3.9+):**
   - PowerShell: `$env:NTFY_TOPIC="YOUR-TOPIC"; python watch.py --test-notify`
   - Mac/Linux: `NTFY_TOPIC=YOUR-TOPIC python3 watch.py --test-notify`
3. **Push to a new public repo and add the topic as a secret** (with the [GitHub CLI](https://cli.github.com)):

   ```bash
   git init -b main
   git add .
   git commit -m "Doomsday watch"
   gh repo create doomsday-watch --public --source . --push
   gh secret set NTFY_TOPIC --body "YOUR-TOPIC"
   gh workflow run watch.yml
   ```

   No CLI? Create the repo on github.com, upload all the files (including the hidden `.github` folder), add the
   secret under Settings → Secrets and variables → Actions, then Actions → Doomsday ticket watch → Run workflow.
4. **Confirm:** within a minute or two you should get **👀 Watching Junxion Erin Mills for Doomsday**, which also
   says how far ahead Junxion's schedule is posted. If tickets are already on sale you get the 🎟️ alert instead,
   and if GitHub can't reach Cineplex you get ⚠️ right away.

Why public: Actions minutes are free and unlimited on public repos; a private repo would burn through the free
2,000 minutes/month in about a week at this frequency. Secrets stay private either way.

## Optional: Discord as well

Create a channel webhook (Server Settings → Integrations → Webhooks → New Webhook → Copy URL), then:

```bash
gh secret set DISCORD_WEBHOOK_URL --body "https://discord.com/api/webhooks/..."
gh secret set DISCORD_USER_ID --body "123456789"  # optional @-mention: Developer Mode → right-click yourself → Copy User ID
```

Discord alerts link each showtime individually.

## Settings

Add these under Settings → Secrets and variables → Actions (or with `gh secret set` / `gh variable set`):

| Name | Kind | What it does |
|---|---|---|
| `NTFY_TOPIC` | secret | Your ntfy topic (a pasted `ntfy.sh/...` URL works too) |
| `DISCORD_WEBHOOK_URL` | secret | Discord channel webhook |
| `DISCORD_USER_ID` | secret | Your Discord user id, so the on-sale alert @-mentions you |
| `NTFY_TOKEN` | secret | Access token, only if you use a password-protected ntfy topic |
| `CINEPLEX_API_KEY` | secret | Overrides Cineplex's public API key; only needed if auto-recovery fails |
| `WATCH_DATES` | variable | Dates to watch, e.g. `2026-12-16..2026-12-27` (default Dec 16–20) |
| `MOVIE_PATTERN` | variable | Regex for the film title (default `avengers.*doomsday`) |

To turn off the 📅 new-showtimes pings, add `WINDOW_ALERTS: off` under `env:` in `.github/workflows/watch.yml`.

## Running it locally

Use `python` on Windows, `python3` on Mac/Linux. Set `NTFY_TOPIC` first, as in setup step 2.

- `python watch.py --dry-run` does one check and prints what it sees (tomorrow's lineup, how far ahead the
  schedule is posted, whether Doomsday is bookable). It sends nothing and saves nothing.
- `python watch.py --test-notify` sends a test alert.
- `python watch.py --every 60` checks every minute until you press Ctrl+C. Handy on the night you expect tickets,
  since GitHub's schedule is best-effort (every 5–15 minutes).

Local runs keep their memory in `state.local.json` (gitignored), separate from the bot's `state.json`.

## How it works

- `watch.py` is the whole bot: Python 3.9+, standard library only.
- `.github/workflows/watch.yml` runs it every 5 minutes and commits `state.json`, which is how the bot remembers
  what it already told you. Don't edit or delete that file.
- `tests/` has offline tests with Cineplex, ntfy and Discord faked: `python -m unittest discover tests`

## Troubleshooting

- **No 👀 ping after the first run:** open the run in the Actions tab and read the "Check Cineplex" step.
  "Set NTFY_TOPIC…" means the secret is missing or misnamed; "ntfy alert failed" points to a typo in the topic.
- **⚠️ can't read Cineplex:** the message says why. If the API key rotated and auto-recovery failed, open
  cineplex.com with DevTools → Network, click any `apis.cineplex.com` request, copy the `Ocp-Apim-Subscription-Key`
  header, and run `gh secret set CINEPLEX_API_KEY --body <key>`.
- **`git push` rejected:** the bot commits `state.json` now and then, so run `git pull --rebase` first.
- **Gaps between runs:** GitHub runs schedules best-effort; 5–15 minutes (occasionally longer) is normal.

## When you're done

`gh workflow disable watch.yml`, or delete the repo. The bot also stops checking once Dec 20 has passed.
