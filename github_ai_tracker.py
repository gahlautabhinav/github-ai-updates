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
    return f"[[{safe_name(full_name)}|{full_name}]]"


def trunc(text, n=140):
    text = " ".join((text or "").split())
    return text if len(text) <= n else text[: n - 1] + "…"


def trending_table(repos):
    if not repos:
        return "_No repos found for this window._\n"
    lines = ["| # | Repo | ⭐ | Created | Lang | Description |",
             "|---|------|----|---------|------|-------------|"]
    for i, r in enumerate(repos, 1):
        lines.append(
            f"| {i} | {wikilink(r['full_name'])} | {r['stars']} | "
            f"{r['created_at']} | {r['language']} | {trunc(r['description'])} |"
        )
    return "\n".join(lines) + "\n"


def growth_table(repos):
    if not repos:
        return "_Not enough snapshot history yet — fills in as the tracker runs daily._\n"
    lines = ["| # | Repo | +⭐ | Now | Created | Lang | Description |",
             "|---|------|-----|-----|---------|------|-------------|"]
    for i, r in enumerate(repos, 1):
        lines.append(
            f"| {i} | {wikilink(r['full_name'])} | +{r['delta']} | {r['stars']} | "
            f"{r['created_at']} | {r['language']} | {trunc(r['description'])} |"
        )
    return "\n".join(lines) + "\n"


def write_daily_note(base, today, week, month, g24, g30):
    daily_dir = os.path.join(base, "Daily")
    os.makedirs(daily_dir, exist_ok=True)
    path = os.path.join(daily_dir, f"{today}.md")
    body = (
        f"---\ntype: ai-repo-digest\ndate: {today}\ntags: [ai-repos, digest]\n---\n\n"
        f"# AI GitHub Repos — {today}\n\n"
        f"## \U0001f525 Trending created this week\n\n{trending_table(week)}\n"
        f"## \U0001f4c5 Trending created this month\n\n{trending_table(month)}\n"
        f"## ⚡ Fastest growing — last 24h\n\n{growth_table(g24)}\n"
        f"## \U0001f4c8 Fastest growing — last 30 days\n\n{growth_table(g30)}\n"
        f"---\n*Generated by personal-ai-repo-tracker at "
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
    hist_tbl = "| Day | ⭐ |\n|-----|----|\n" + "\n".join(
        f"| {d} | {s} |" for d, s in hist
    )
    topics = r.get("topics")
    if isinstance(topics, str):
        topics = [t for t in topics.split(",") if t]
    topics = topics or []

    body = (
        f"---\nrepo: {full_name}\nurl: {r['html_url']}\nstars: {r.get('stars', '')}\n"
        f"created: {r['created_at']}\nlanguage: {r['language']}\n"
        f"topics: [{', '.join(topics)}]\nupdated: {today}\ntags: [ai-repo]\n---\n\n"
        f"# {full_name}\n\n"
        f"> {trunc(r['description'], 400) or '_no description_'}\n\n"
        f"\U0001f517 {r['html_url']}\n\n"
        f"**Stars:** {r.get('stars', '?')} · **Created:** {r['created_at']} "
        f"· **Language:** {r['language'] or 'n/a'}\n\n"
        f"## Star history (last 14 snapshots)\n\n{hist_tbl}\n"
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
