# 🛰️ AI Repo Tracker → Obsidian

A personal, self-hosted **daily digest of trending and fastest-growing AI GitHub repos**, written straight into an Obsidian vault as a styled "console" dashboard. Keep your finger on the pulse of new AI tools — go to the source instead of waiting for the downstream videos.

stdlib-only Python. No `pip install`. No external services. The only thing that touches the network is the GitHub REST API.

> Inspired by a YouTuber's morning-automation idea, rebuilt as a personal tool with an Obsidian-native UI and Claude-written video-idea summaries.

---

## What it produces

Every morning, in `<vault>/AI Repos/`:

| Note | Contents |
|------|----------|
| `Daily/YYYY-MM-DD.md` | Dashboard with 4 ranked tables (below) + a Claude `## TL;DR — video ideas` section |
| `Repos/owner__name.md` | One note per surfaced repo: description, GitHub link, topics, and a growing star-history table |

The dashboard's four lists:

- 🔥 **Top 10 trending — created this week** (by stars)
- 📅 **Top 5 trending — created this month**
- ⚡ **Top 10 fastest growing — last 24h** (star velocity)
- 📈 **Top 10 fastest growing — last 30 days**

Notes are wikilinked into your Obsidian graph and carry `cssclasses` so a bundled CSS snippet renders them as a dark, data-forward dashboard. **View in Reading mode (`Ctrl+E`) for the full look.**

> ⏳ The two **growth** lists need accrued history: the 24h list appears after the 2nd daily run, the 30d list fills in over ~a month. Trending lists work on day 1.

---

## How it works

```
GitHub Search API ──► merge/dedupe across AI topics ──► snapshots.db (SQLite)
                                                              │
                          ┌───────────────────────────────────┤
                  trending (created in window)        growth (diff today vs prior snapshot)
                          └───────────────────┬───────────────┘
                                              ▼
                       Markdown notes + per-repo notes  ──►  Obsidian vault
                                              │
                                  optional: claude -p  ──►  TL;DR appended
```

- **Trending** = repos *created* in the window, ranked by stars (new & hot).
- **Growth** = star velocity for repos of *any* age (catches old repos that suddenly take off) — computed by diffing today's star counts against an earlier daily snapshot stored in SQLite.
- **Styling** = a CSS snippet auto-written to `<vault>/.obsidian/snippets/ai-repos.css` and auto-enabled.

See **[ARCHITECTURE.md](ARCHITECTURE.md)** for the full design.

---

## Quick start

1. **GitHub token** (lifts the rate limit to 5000/hr): https://github.com/settings/tokens → *Generate new token (classic)* → **check no scopes** (public search needs none) → copy.
2. `copy config.example.json config.json`, then edit `config.json`:
   - `vault_path` → absolute path to your Obsidian vault
   - `github_token` → paste the token (or set env var `GITHUB_TOKEN` — it wins over the file)
   - `topics` → tune what "AI" means to you
3. First run:
   ```
   py -3.10 github_ai_tracker.py
   ```
   Open `<vault>/AI Repos/Daily/` in Obsidian (Reading view).

`config.json`, `snapshots.db`, and the vault are gitignored — your token never leaves your machine.

---

## Run it every morning (Windows Task Scheduler)

One command creates the daily job (no GUI):

```
schtasks /create /tn "AI Repo Tracker" /tr "<repo>\run_daily.bat" /sc daily /st 08:00 /f
```

Optionally make it catch up if the laptop was off at 8am:

```powershell
$s=(Get-ScheduledTask -TaskName 'AI Repo Tracker').Settings; $s.StartWhenAvailable=$true; Set-ScheduledTask -TaskName 'AI Repo Tracker' -Settings $s
```

The job is **local** — the laptop must be on (or get turned on later that day, thanks to catch-up). A cloud schedule can't reach a local vault. Logs append to `run.log`.

---

## Configuration (`config.json`)

| Key | Meaning |
|-----|---------|
| `vault_path` | Absolute path to your Obsidian vault |
| `subfolder` | Folder inside the vault for output (default `AI Repos`) |
| `github_token` | GitHub PAT (or use `GITHUB_TOKEN` env var) |
| `topics` | GitHub topics treated as "AI" |
| `trending_week_count` / `trending_month_count` | Sizes of the trending lists |
| `growth_24h_count` / `growth_30d_count` | Sizes of the growth lists |
| `universe_per_topic` | How many top repos per topic to snapshot for growth tracking |
| `min_stars_universe` | Floor for the snapshot universe |
| `claude_summary` | `true` → append a Claude-written TL;DR via the local `claude` CLI |

---

## Self-check

```
py -3.10 github_ai_tracker.py --selftest
```

Verifies the growth-diff + ranking logic on fixture data.

---

## Tech

- **Python 3.10**, standard library only (`urllib`, `sqlite3`, `subprocess`, `json`).
- **GitHub REST Search API** for data (not the `gh` CLI).
- **SQLite** (`snapshots.db`) for star history.
- **Obsidian** for the UI (markdown + a CSS snippet).
- **Claude CLI** (optional) for the TL;DR.
