#!/usr/bin/env python3
"""
Personal AI-repo tracker -> Obsidian vault.

Runs daily. Hits GitHub Search API, builds 4 lists:
  - Top trending created this WEEK   (by stars)
  - Top trending created this MONTH  (by stars)
  - Fastest growing last 24h         (star delta, needs >=2 days history)
  - Fastest growing last 30d         (star delta, needs ~30 days history)

Writes a daily dashboard note + one note per surfaced repo into your vault.
Star counts for a wider universe are snapshotted into SQLite so growth can be
diffed over time. stdlib only -- no pip install needed.

Usage:
  py -3.10 github_ai_tracker.py            # normal daily run
  py -3.10 github_ai_tracker.py --selftest # run the growth-diff self-check
"""

import json
import os
import sqlite3
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(HERE, "config.json")
DB_PATH = os.path.join(HERE, "snapshots.db")
API = "https://api.github.com/search/repositories"
SEARCH_DELAY = 2.0  # seconds between search calls (search has a tight rate limit)

# Console-dashboard styling. Written to <vault>/.obsidian/snippets/ and applied
# only to notes carrying  cssclasses: [ai-digest] / [ai-repo].  raw string so the
# CSS unicode escapes (\2605 etc) survive into the file verbatim.
CSS_SNIPPET = r"""/* ============================================================
   AI Repos — dark console dashboard for the daily digest.
   Auto-written + auto-enabled by github_ai_tracker.py.
   Scoped to notes with  cssclasses: [ai-digest] / [ai-repo].
   ============================================================ */

.ai-digest, .ai-repo {
  --air-bg:      oklch(0.165 0.012 264);
  --air-surface: oklch(0.225 0.016 264);
  --air-ink:     oklch(0.945 0.008 264);
  --air-muted:   oklch(0.760 0.012 264);
  --air-faint:   oklch(0.620 0.012 264);
  --air-border:  oklch(0.315 0.012 264);
  --air-link:    oklch(0.820 0.105 232);
  --air-star:    oklch(0.845 0.130 86);
  --air-grow:    oklch(0.820 0.150 152);

  background: var(--air-bg);
  color: var(--air-ink);
  -webkit-font-smoothing: antialiased;
}

/* let the dashboard use full width (override Obsidian readable-line-length) */
.ai-digest .markdown-preview-sizer,
.ai-repo  .markdown-preview-sizer {
  max-width: none !important; padding: 2.2rem 2.6rem;
}

/* hide the redundant filename inline-title; the banner h1 carries it */
.ai-digest .inline-title, .ai-repo .inline-title { display: none; }

/* hide the frontmatter Properties block on the dashboard for a clean console look */
.ai-digest .metadata-container, .ai-repo .metadata-container { display: none !important; }

/* widen Live Preview too (Reading view handled via .markdown-preview-sizer above) */
.markdown-source-view.ai-digest .cm-sizer,
.markdown-source-view.ai-repo  .cm-sizer { max-width: none !important; }

/* banner title */
.ai-digest h1, .ai-repo h1 {
  font-weight: 700; letter-spacing: -0.02em; color: var(--air-ink);
  margin: 0 0 1.4rem; text-wrap: balance;
}
.ai-digest h1 { font-size: 1.65rem; }
.ai-repo  h1 { font-size: 1.4rem; margin-bottom: 0.6rem; }

/* section headers: accent dot + hairline, sentence case (no eyebrow scaffold) */
.ai-digest h2 {
  display: flex; align-items: center; gap: 0.55rem;
  font-size: 0.98rem; font-weight: 650; letter-spacing: -0.01em;
  color: var(--air-ink); margin: 2.1rem 0 0.55rem;
  padding-bottom: 0.5rem; border-bottom: 1px solid var(--air-border);
}
.ai-digest h2::before {
  content: ""; flex: none; width: 7px; height: 7px; border-radius: 50%;
  background: var(--air-link);
  box-shadow: 0 0 0 3px color-mix(in oklch, var(--air-link) 22%, transparent);
}

/* data tables */
.ai-digest table, .ai-repo table {
  width: 100%; border-collapse: collapse; margin: 0.2rem 0 0.4rem; font-size: 0.9rem;
}
.ai-digest thead th, .ai-repo thead th {
  text-align: left; padding: 0 0.7rem 0.55rem; border: none;
  font-size: 0.68rem; font-weight: 600; text-transform: uppercase;
  letter-spacing: 0.07em; color: var(--air-faint);
}
.ai-digest tbody td, .ai-repo tbody td {
  padding: 0.62rem 0.7rem; border: none; border-top: 1px solid var(--air-border);
  color: var(--air-muted); vertical-align: baseline;
}
.ai-digest tbody tr:hover td, .ai-repo tbody tr:hover td { background: var(--air-surface); }
.ai-digest tbody td:nth-child(2) a {
  color: var(--air-link); font-weight: 600; text-decoration: none;
}
.ai-digest tbody td:nth-child(2) a:hover { text-decoration: underline; }

/* column coloring (pure-markdown tables, so wikilinks still resolve).
   digest columns:  1=#  2=Repo  3=Stars  4=Δ  5=Lang  6=Created  7=What */
.ai-digest tbody td:nth-child(1) {
  color: var(--air-faint); font-weight: 600;
  font-variant-numeric: tabular-nums; text-align: right; white-space: nowrap;
}
.ai-digest tbody td:nth-child(3) {
  color: var(--air-star); font-variant-numeric: tabular-nums;
  text-align: right; white-space: nowrap !important;
}
.ai-digest tbody td:nth-child(3):not(:empty)::before { content: "\2605\00a0"; }
.ai-digest tbody td:nth-child(4) {  /* Δ — literal ▲ comes from the row text, only on growth */
  color: var(--air-grow); font-weight: 650;
  font-variant-numeric: tabular-nums; text-align: right; white-space: nowrap;
}
.ai-digest tbody td:nth-child(5) { color: var(--air-muted); font-size: 0.82rem; white-space: nowrap; }
.ai-digest tbody td:nth-child(6) {
  color: var(--air-faint); font-size: 0.82rem;
  font-variant-numeric: tabular-nums; white-space: nowrap;
}
.ai-digest tbody td:nth-child(7) { color: var(--air-muted); max-width: 42ch; }

/* repo-note star history:  1=Day  2=Stars */
.ai-repo tbody td:nth-child(1) {
  color: var(--air-faint); font-variant-numeric: tabular-nums;
}
.ai-repo tbody td:nth-child(2) {
  color: var(--air-star); font-variant-numeric: tabular-nums; text-align: right;
}
.ai-repo tbody td:nth-child(2)::before { content: "\2605\00a0"; }

/* empty state + footer */
.ai-digest p em { color: var(--air-faint); font-style: normal; }
.ai-digest hr, .ai-repo hr {
  border: none; border-top: 1px solid var(--air-border); margin: 2rem 0 1rem;
}

/* repo note: description as a clean panel (no side-stripe) */
.ai-repo blockquote {
  border: none; background: var(--air-surface); border-radius: 8px;
  padding: 0.9rem 1.1rem; margin: 0 0 1.2rem; color: var(--air-ink); font-style: normal;
}
.ai-repo strong { color: var(--air-star); font-weight: 700; }  /* the ★ stat line */
.ai-repo a { color: var(--air-link); text-decoration: none; }
.ai-repo a:hover { text-decoration: underline; }
.ai-repo h2 {
  display: flex; align-items: center; gap: 0.55rem;
  font-size: 0.95rem; font-weight: 650; color: var(--air-ink);
  margin: 1.8rem 0 0.5rem; padding-bottom: 0.45rem;
  border-bottom: 1px solid var(--air-border);
}
.ai-repo h2::before {
  content: ""; flex: none; width: 7px; height: 7px; border-radius: 50%;
  background: var(--air-star);
}
/* the GitHub link rendered as a quiet button */
.ai-repo p > a[href^="http"] {
  display: inline-block; margin: 0.1rem 0 0.5rem; padding: 0.35rem 0.8rem;
  border: 1px solid var(--air-border); border-radius: 7px;
  color: var(--air-link); font-size: 0.85rem;
}
.ai-repo p > a[href^="http"]:hover { border-color: var(--air-link); text-decoration: none; }
"""


def ensure_obsidian_theme(vault_path):
    """Write the CSS snippet and best-effort auto-enable it in the vault."""
    obs = os.path.join(vault_path, ".obsidian")
    snippets = os.path.join(obs, "snippets")
    os.makedirs(snippets, exist_ok=True)
    css_path = os.path.join(snippets, "ai-repos.css")
    old = ""
    if os.path.exists(css_path):
        with open(css_path, "r", encoding="utf-8") as f:
            old = f.read()
    if old != CSS_SNIPPET:
        with open(css_path, "w", encoding="utf-8") as f:
            f.write(CSS_SNIPPET)
        print("  wrote CSS snippet -> .obsidian/snippets/ai-repos.css")
    # auto-enable in appearance.json, preserving any existing keys
    app_path = os.path.join(obs, "appearance.json")
    try:
        data = {}
        if os.path.exists(app_path):
            with open(app_path, "r", encoding="utf-8") as f:
                data = json.load(f)
        enabled = data.get("enabledCssSnippets", [])
        if "ai-repos" not in enabled:
            enabled.append("ai-repos")
            data["enabledCssSnippets"] = enabled
            with open(app_path, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2)
            print("  enabled snippet in appearance.json")
    except Exception as e:  # ponytail: nice-to-have; user can toggle it manually
        print(f"  (enable 'ai-repos' yourself in Settings > Appearance > CSS snippets: {e})")


# ---------------------------------------------------------------- config / db
def load_config():
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        cfg = json.load(f)
    # env var wins over file so you can keep the token out of the repo
    cfg["github_token"] = os.environ.get("GITHUB_TOKEN") or cfg.get("github_token", "")
    return cfg


def db_connect():
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        """CREATE TABLE IF NOT EXISTS repos(
            repo_id INTEGER PRIMARY KEY,
            full_name TEXT, html_url TEXT, description TEXT,
            created_at TEXT, language TEXT, topics TEXT, last_stars INTEGER)"""
    )
    conn.execute(
        """CREATE TABLE IF NOT EXISTS snapshots(
            repo_id INTEGER, day TEXT, stars INTEGER,
            PRIMARY KEY(repo_id, day))"""
    )
    conn.commit()
    return conn


# --------------------------------------------------------------- github calls
def _get_json(url, token, query, page, retries=4):
    """One GET with retry on transient network errors + rate-limit backoff."""
    for attempt in range(1, retries + 1):
        req = urllib.request.Request(url)
        req.add_header("Accept", "application/vnd.github+json")
        req.add_header("User-Agent", "personal-ai-repo-tracker")
        if token and not token.startswith("PUT_YOUR"):
            req.add_header("Authorization", f"Bearer {token}")
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            if e.code in (403, 429):  # rate limited (primary or secondary)
                wait = int(e.headers.get("Retry-After", "60"))
                print(f"  rate limited on '{query}' p{page} -> sleeping {wait}s")
                time.sleep(wait)
                continue
            print(f"  HTTP {e.code} on '{query}': {e.reason}")
            return None
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            wait = 5 * attempt  # transient: timeout / DNS / dropped connection
            reason = getattr(e, "reason", e)
            print(f"  net error on '{query}' p{page} "
                  f"(try {attempt}/{retries}): {reason} -> retry in {wait}s")
            time.sleep(wait)
            continue
    print(f"  gave up on '{query}' p{page} after {retries} tries")
    return None


def gh_search(query, token, sort="stars", order="desc", per_page=30, pages=1):
    """Return a list of repo item dicts for a search query (paginated, throttled)."""
    items = []
    for page in range(1, pages + 1):
        qs = urllib.parse.urlencode(
            {"q": query, "sort": sort, "order": order,
             "per_page": per_page, "page": page}
        )
        data = _get_json(f"{API}?{qs}", token, query, page)
        if data is None:
            break
        batch = data.get("items", [])
        items.extend(batch)
        if len(batch) < per_page:
            break  # no more results
        time.sleep(SEARCH_DELAY)
    return items


def normalize(item):
    return {
        "repo_id": item["id"],
        "full_name": item["full_name"],
        "html_url": item["html_url"],
        "description": (item.get("description") or "").strip(),
        "created_at": (item.get("created_at") or "")[:10],
        "language": item.get("language") or "",
        "topics": item.get("topics") or [],
        "stars": item.get("stargazers_count", 0),
    }


def merge_by_id(lists):
    """Flatten search result lists, keep the highest-star copy of each repo."""
    out = {}
    for lst in lists:
        for it in lst:
            r = normalize(it)
            cur = out.get(r["repo_id"])
            if cur is None or r["stars"] > cur["stars"]:
                out[r["repo_id"]] = r
    return out


# --------------------------------------------------------------- snapshotting
def snapshot(conn, repos_by_id, today):
    for r in repos_by_id.values():
        conn.execute(
            """INSERT INTO repos(repo_id,full_name,html_url,description,
                created_at,language,topics,last_stars)
               VALUES(?,?,?,?,?,?,?,?)
               ON CONFLICT(repo_id) DO UPDATE SET
                 full_name=excluded.full_name, html_url=excluded.html_url,
                 description=excluded.description, created_at=excluded.created_at,
                 language=excluded.language, topics=excluded.topics,
                 last_stars=excluded.last_stars""",
            (r["repo_id"], r["full_name"], r["html_url"], r["description"],
             r["created_at"], r["language"], ",".join(r["topics"]), r["stars"]),
        )
        conn.execute(
            "INSERT OR REPLACE INTO snapshots(repo_id,day,stars) VALUES(?,?,?)",
            (r["repo_id"], today, r["stars"]),
        )
    conn.commit()


def reference_day(conn, today, target_days_ago):
    """Snapshot day nearest to (today - target_days_ago), among days before today."""
    rows = conn.execute(
        "SELECT DISTINCT day FROM snapshots WHERE day < ?", (today,)
    ).fetchall()
    if not rows:
        return None
    t0 = datetime.strptime(today, "%Y-%m-%d").date()
    target = t0 - timedelta(days=target_days_ago)
    best, best_gap = None, None
    for (d,) in rows:
        gap = abs((datetime.strptime(d, "%Y-%m-%d").date() - target).days)
        if best_gap is None or gap < best_gap:
            best, best_gap = d, gap
    return best


def growth(conn, today, ref_day, limit):
    """Repos ranked by (stars_today - stars_ref_day), positive deltas only."""
    if ref_day is None:
        return []
    rows = conn.execute(
        """SELECT r.full_name, r.html_url, r.description, r.created_at,
                  r.language, t.stars AS now, y.stars AS was, t.stars - y.stars AS delta
           FROM snapshots t
           JOIN snapshots y ON y.repo_id = t.repo_id AND y.day = ?
           JOIN repos r ON r.repo_id = t.repo_id
           WHERE t.day = ? AND (t.stars - y.stars) > 0
           ORDER BY delta DESC LIMIT ?""",
        (ref_day, today, limit),
    ).fetchall()
    return [dict(zip(
        ["full_name", "html_url", "description", "created_at",
         "language", "stars", "was", "delta"], row)) for row in rows]


# ------------------------------------------------------------------- markdown
def safe_name(full_name):
    return full_name.replace("/", "__")


def wikilink(full_name):
    # escape the pipe so the alias separator isn't read as a table-cell delimiter
    return f"[[{safe_name(full_name)}\\|{full_name}]]"


def trunc(text, n=140):
    # replace pipes -> table cells would break on them
    text = " ".join((text or "").replace("|", "/").split())
    return text if len(text) <= n else text[: n - 1] + "…"


def trending_table(repos):
    if not repos:
        return "_No new repos in this window._\n"
    # uniform 7-col shape (Δ blank here) so CSS column coloring matches growth tables
    lines = ["| # | Repo | Stars | Δ | Lang | Created | What it is |",
             "|--:|------|------:|--:|------|---------|------------|"]
    for i, r in enumerate(repos, 1):
        lines.append(
            f"| {i} | {wikilink(r['full_name'])} | {r['stars']} |  | "
            f"{r['language'] or '—'} | {r['created_at']} | {trunc(r['description'])} |"
        )
    return "\n".join(lines) + "\n"


def growth_table(repos):
    if not repos:
        return ("_Not enough history yet — this list fills in as the tracker "
                "runs each day._\n")
    lines = ["| # | Repo | Stars | Δ | Lang | Created | What it is |",
             "|--:|------|------:|--:|------|---------|------------|"]
    for i, r in enumerate(repos, 1):
        lines.append(
            f"| {i} | {wikilink(r['full_name'])} | {r['stars']} | ▲ +{r['delta']} | "
            f"{r['language'] or '—'} | {r['created_at']} | {trunc(r['description'])} |"
        )
    return "\n".join(lines) + "\n"


def write_daily_note(base, today, week, month, g24, g30):
    daily_dir = os.path.join(base, "Daily")
    os.makedirs(daily_dir, exist_ok=True)
    path = os.path.join(daily_dir, f"{today}.md")
    body = (
        f"---\ncssclasses: [ai-digest]\ntype: ai-repo-digest\ndate: {today}\n"
        f"tags: [ai-repos, digest]\n---\n\n"
        f"# AI GitHub Repos · {today}\n\n"
        f"## Trending · created this week\n\n{trending_table(week)}\n"
        f"## Trending · created this month\n\n{trending_table(month)}\n"
        f"## Fastest growing · last 24 hours\n\n{growth_table(g24)}\n"
        f"## Fastest growing · last 30 days\n\n{growth_table(g30)}\n"
        f"---\n*Generated by personal-ai-repo-tracker · "
        f"{datetime.now().strftime('%Y-%m-%d %H:%M')}*\n"
    )
    with open(path, "w", encoding="utf-8") as f:
        f.write(body)
    return path


def maybe_claude_summary(daily_path, cfg):
    """If enabled, ask the local Claude CLI to append a video-idea TL;DR."""
    if not cfg.get("claude_summary"):
        return
    prompt = (
        f"Read the file '{daily_path}'. Append a '## TL;DR — video ideas' section "
        f"at the end: 4-6 bullets on the most notable repos in it and why a content "
        f"creator should care / what's testable. Edit the file in place, keep it tight."
    )
    print("Generating Claude summary...")
    try:
        # shell=True so Windows resolves the `claude` / `claude.cmd` launcher
        subprocess.run(f'claude -p "{prompt}"', shell=True, timeout=300, check=False)
    except Exception as e:  # ponytail: summary is a nice-to-have, never fail the run over it
        print(f"  summary step skipped: {e}")


def write_repo_note(conn, base, r, today):
    repo_dir = os.path.join(base, "Repos")
    os.makedirs(repo_dir, exist_ok=True)
    full_name = r["full_name"]
    path = os.path.join(repo_dir, f"{safe_name(full_name)}.md")

    hist = conn.execute(
        "SELECT day, stars FROM snapshots WHERE repo_id="
        "(SELECT repo_id FROM repos WHERE full_name=?) ORDER BY day DESC LIMIT 14",
        (full_name,),
    ).fetchall()
    hist_tbl = "| Day | Stars |\n|-----|------:|\n" + "\n".join(
        f"| {d} | {s} |" for d, s in hist
    )
    topics = r.get("topics")
    if isinstance(topics, str):
        topics = [t for t in topics.split(",") if t]
    topics = topics or []

    body = (
        f"---\ncssclasses: [ai-repo]\nrepo: {full_name}\nurl: {r['html_url']}\n"
        f"stars: {r.get('stars', '')}\n"
        f"created: {r['created_at']}\nlanguage: {r['language']}\n"
        f"topics: [{', '.join(topics)}]\nupdated: {today}\ntags: [ai-repo]\n---\n\n"
        f"# {full_name}\n\n"
        f"> {trunc(r['description'], 400) or 'no description'}\n\n"
        f"**★ {r.get('stars', '?')}**  ·  {r['language'] or '—'}  ·  created {r['created_at']}\n\n"
        f"[Open on GitHub →]({r['html_url']})\n\n"
        + (f"Topics — {' · '.join(topics)}\n\n" if topics else "")
        + f"## Star history (last 14 snapshots)\n\n{hist_tbl}\n"
    )
    with open(path, "w", encoding="utf-8") as f:
        f.write(body)


# ----------------------------------------------------------------------- main
def run():
    cfg = load_config()
    if cfg["vault_path"].startswith("PUT_YOUR"):
        sys.exit("ERROR: set vault_path in config.json first.")
    token = cfg["github_token"]
    if not token or token.startswith("PUT_YOUR"):
        print("WARNING: no GitHub token -> 60 req/hr limit, run may be throttled.\n")

    base = os.path.join(cfg["vault_path"], cfg["subfolder"])
    os.makedirs(base, exist_ok=True)
    ensure_obsidian_theme(cfg["vault_path"])
    today = date.today().isoformat()
    wk = (date.today() - timedelta(days=7)).isoformat()
    mo = (date.today() - timedelta(days=30)).isoformat()
    min_uni = cfg.get("min_stars_universe", 50)
    uni_pages = max(1, cfg["universe_per_topic"] // 30)

    conn = db_connect()

    # 1) trending = newly-created repos ranked by stars (merged across topics)
    print("Fetching trending (week / month)...")
    week_lists, month_lists, universe_lists = [], [], []
    for t in cfg["topics"]:
        print(f"  topic: {t}")
        week_lists.append(gh_search(f"topic:{t} created:>={wk}", token, pages=1))
        month_lists.append(gh_search(f"topic:{t} created:>={mo}", token, pages=1))
        universe_lists.append(
            gh_search(f"topic:{t} stars:>={min_uni}", token, pages=uni_pages)
        )

    week = sorted(merge_by_id(week_lists).values(),
                  key=lambda r: r["stars"], reverse=True)[: cfg["trending_week_count"]]
    month = sorted(merge_by_id(month_lists).values(),
                   key=lambda r: r["stars"], reverse=True)[: cfg["trending_month_count"]]

    # 2) snapshot the universe + trending repos so growth can be diffed over time
    universe = merge_by_id(universe_lists + week_lists + month_lists)
    print(f"Snapshotting {len(universe)} repos for {today}...")
    snapshot(conn, universe, today)

    # 3) growth lists from snapshot history
    ref24 = reference_day(conn, today, 1)
    ref30 = reference_day(conn, today, 30)
    g24 = growth(conn, today, ref24, cfg["growth_24h_count"])
    g30 = growth(conn, today, ref30, cfg["growth_30d_count"])
    print(f"  24h ref day: {ref24} ({len(g24)} growers) | "
          f"30d ref day: {ref30} ({len(g30)} growers)")

    # 4) write vault notes (daily dashboard + one note per surfaced repo)
    daily_path = write_daily_note(base, today, week, month, g24, g30)
    surfaced = {r["full_name"]: r for r in (week + month + g24 + g30)}
    for r in surfaced.values():
        write_repo_note(conn, base, r, today)

    conn.close()
    maybe_claude_summary(daily_path, cfg)
    print(f"\nDone. {len(surfaced)} repo notes + daily dashboard:\n  {daily_path}")


# ----------------------------------------------------------------- self-check
def selftest():
    """In-memory check that growth diff + ranking is correct."""
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE repos(repo_id INTEGER PRIMARY KEY, full_name TEXT,"
                 " html_url TEXT, description TEXT, created_at TEXT, language TEXT,"
                 " topics TEXT, last_stars INTEGER)")
    conn.execute("CREATE TABLE snapshots(repo_id INTEGER, day TEXT, stars INTEGER,"
                 " PRIMARY KEY(repo_id,day))")
    data = [  # repo_id, yesterday_stars, today_stars
        (1, "a/fast", 100, 400),   # +300
        (2, "b/slow", 100, 110),   # +10
        (3, "c/flat", 50, 50),     # 0  -> excluded
        (4, "d/new", None, 200),   # no prior snapshot -> excluded
    ]
    for rid, name, y, td in data:
        conn.execute("INSERT INTO repos VALUES(?,?,?,?,?,?,?,?)",
                     (rid, name, "", "", "2025-01-01", "Py", "", td))
        if y is not None:
            conn.execute("INSERT INTO snapshots VALUES(?,?,?)", (rid, "2026-06-26", y))
        conn.execute("INSERT INTO snapshots VALUES(?,?,?)", (rid, "2026-06-27", td))
    conn.commit()

    ref = reference_day(conn, "2026-06-27", 1)
    assert ref == "2026-06-26", ref
    g = growth(conn, "2026-06-27", ref, 10)
    names = [r["full_name"] for r in g]
    assert names == ["a/fast", "b/slow"], names          # flat + new excluded, sorted by delta
    assert g[0]["delta"] == 300 and g[1]["delta"] == 10, g
    print("selftest OK")


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        selftest()
    else:
        run()
