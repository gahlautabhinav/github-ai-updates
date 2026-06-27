# Personal AI-Repo Tracker → Obsidian

Daily digest of trending + fastest-growing AI GitHub repos, written straight
into your Obsidian vault. stdlib-only Python, no `pip install`.

## What it produces (in `<vault>/AI Repos/`)

- `Daily/YYYY-MM-DD.md` — dashboard note with 4 tables:
  - 🔥 Top 10 trending created **this week** (by stars)
  - 📅 Top 5 trending created **this month**
  - ⚡ Top 10 fastest growing **last 24h** (star delta)
  - 📈 Top 10 fastest growing **last 30 days**
- `Repos/owner__name.md` — one note per surfaced repo, with frontmatter,
  description, GitHub link, and a star-history table. Wikilinked from the
  daily note so it grows your graph over time.

Star counts for a wider universe are stored in `snapshots.db` (SQLite) so the
growth lists can be diffed across days. **24h list needs ≥2 daily runs; 30d
list fills in over ~a month.**

## Setup (2 min)

1. **GitHub token** (5000 req/hr): https://github.com/settings/tokens →
   "Generate new token (classic)" → no scopes needed for public search → copy.
2. `copy config.example.json config.json` then edit `config.json`
   (it's gitignored — your token stays local, never pushed):
   - `vault_path` → absolute path to your Obsidian vault, e.g.
     `C:/Users/abhin/Documents/MyVault`
   - `github_token` → paste the token (or leave it and set env var
     `GITHUB_TOKEN` instead — env wins, keeps token out of the file)
   - `topics` → tune what "AI" means to you (default = broad AI)
3. Test run:
   ```
   py -3.10 github_ai_tracker.py
   ```
   Check `<vault>/AI Repos/Daily/` for today's note.

## Schedule it every morning (Windows Task Scheduler)

1. Open **Task Scheduler** → **Create Basic Task**.
2. Name: `AI Repo Tracker`. Trigger: **Daily**, time `08:00`.
3. Action: **Start a program** → Program/script: browse to
   `D:\Agents_learn\github_ai_tool\run_daily.bat`.
4. Finish. (In task properties tick "Run whether user is logged on or not" if
   you want it to fire while logged out.)

Logs append to `run.log`.

## Optional: Claude-written TL;DR (for video ideas)

Set `"claude_summary": true` in `config.json`. After the data run, the script
calls your local Claude CLI to append a `## TL;DR — video ideas` section to the
daily note (most notable repos, why a creator should care, what's testable).
Off by default so first runs are fast and pure-data; flip it on when ready.

## Self-check

```
py -3.10 github_ai_tracker.py --selftest
```
Verifies the growth-diff + ranking logic on fake data.

## Notes / ceilings

- Trending = repos *created* in the window, ranked by stars (matches "new and
  hot"). Growth = star velocity for *any* age repo (catches old repos that
  suddenly take off).
- Search API merges ~8 topic queries with a 2s throttle → a run takes ~1–2 min.
- Want a cloud Claude Max routine instead of Task Scheduler? Only works if your
  vault is a **git repo** (Obsidian Git plugin); the cloud agent can't touch a
  local-only `D:\` vault.
