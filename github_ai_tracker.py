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

import base64
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
REPO_API = "https://api.github.com/repos/"
SEARCH_DELAY = 2.0  # seconds between search calls (search has a tight rate limit)
DEFAULT_EXCLUDE_TOPICS = {"awesome", "awesome-list", "awesome-lists"}
EMPTY_WATCH = "_Watchlist empty._\n"

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
    # migration: first_seen = the day a repo first entered our tracking
    cols = [c[1] for c in conn.execute("PRAGMA table_info(repos)").fetchall()]
    if "first_seen" not in cols:
        conn.execute("ALTER TABLE repos ADD COLUMN first_seen TEXT")
    conn.commit()
    return conn


# --------------------------------------------------------------- github calls
TOKEN_EXPIRES = None   # raw expiry header from GitHub, e.g. "2026-12-25 09:19:36 UTC"
TOKEN_WARN_DAYS = 14


def token_warning(today, expires=None):
    """Obsidian callout once the token is within TOKEN_WARN_DAYS of expiry, else ''."""
    expires = expires or TOKEN_EXPIRES
    if not expires:
        return ""
    left = (date.fromisoformat(expires[:10]) - date.fromisoformat(today)).days
    if left > TOKEN_WARN_DAYS:
        return ""
    return (f"> [!warning] GitHub token expires in {left} day(s) ({expires[:10]})\n"
            f"> Regenerate it and update `github_token` in config.json, "
            f"or the tracker stops.\n\n")


def _get_json(url, token, query, page, retries=4, quiet=False):
    """One GET with retry on transient network errors + rate-limit backoff.

    quiet=True suppresses HTTP-error logging (for optional endpoints that 404,
    e.g. a repo with no releases).
    """
    for attempt in range(1, retries + 1):
        req = urllib.request.Request(url)
        req.add_header("Accept", "application/vnd.github+json")
        req.add_header("User-Agent", "personal-ai-repo-tracker")
        if token and not token.startswith("PUT_YOUR"):
            req.add_header("Authorization", f"Bearer {token}")
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                global TOKEN_EXPIRES  # header absent for tokens with no expiry
                TOKEN_EXPIRES = (resp.headers.get(
                    "github-authentication-token-expiration") or TOKEN_EXPIRES)
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            if e.code in (403, 429):  # rate limited (primary or secondary)
                wait = int(e.headers.get("Retry-After", "60"))
                print(f"  rate limited on '{query}' p{page} -> sleeping {wait}s")
                time.sleep(wait)
                continue
            if e.code == 401:
                # A dead token fails every call; carrying on writes empty notes
                # and an empty snapshot day while the task still reports success.
                sys.exit("ERROR: GitHub rejected the token (401 Bad credentials) - "
                         "it has expired or been revoked. Put a new one in "
                         "config.json (github_token) or GITHUB_TOKEN.")
            if not quiet:
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
                created_at,language,topics,last_stars,first_seen)
               VALUES(?,?,?,?,?,?,?,?,?)
               ON CONFLICT(repo_id) DO UPDATE SET
                 full_name=excluded.full_name, html_url=excluded.html_url,
                 description=excluded.description, created_at=excluded.created_at,
                 language=excluded.language, topics=excluded.topics,
                 last_stars=excluded.last_stars""",
            (r["repo_id"], r["full_name"], r["html_url"], r["description"],
             r["created_at"], r["language"], ",".join(r["topics"]), r["stars"],
             today),  # first_seen only set on INSERT; preserved on conflict
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
                  r.language, t.stars AS now, y.stars AS was,
                  t.stars - y.stars AS delta, r.topics
           FROM snapshots t
           JOIN snapshots y ON y.repo_id = t.repo_id AND y.day = ?
           JOIN repos r ON r.repo_id = t.repo_id
           WHERE t.day = ? AND (t.stars - y.stars) > 0
           ORDER BY delta DESC LIMIT ?""",
        (ref_day, today, limit),
    ).fetchall()
    return [dict(zip(
        ["full_name", "html_url", "description", "created_at",
         "language", "stars", "was", "delta", "topics"], row)) for row in rows]


# ------------------------------------------------------- signal / discovery
def _topics_list(r):
    t = r.get("topics") or []
    if isinstance(t, str):
        t = [x for x in t.split(",") if x]
    return t


def is_noise(r, cfg):
    """True for awesome-lists / curated-list repos (not runnable tools)."""
    if not cfg.get("filter_noise", True):
        return False
    excl = set(cfg.get("exclude_topics", DEFAULT_EXCLUDE_TOPICS))
    if excl & set(_topics_list(r)):
        return True
    name = r["full_name"].split("/")[-1].lower()
    if name.startswith("awesome-") or name == "awesome":
        return True
    desc = (r.get("description") or "").lower()
    return "awesome list" in desc or "curated list" in desc


def drop_noise(repos, cfg, limit=None):
    out = [r for r in repos if not is_noise(r, cfg)]
    return out[:limit] if limit else out


def age_days(created_at, today):
    try:
        c = datetime.strptime((created_at or "")[:10], "%Y-%m-%d").date()
        t = datetime.strptime(today, "%Y-%m-%d").date()
        return max((t - c).days, 1)
    except ValueError:
        return 1


def add_momentum(repos, today):
    """Stars per day since creation — a velocity proxy that needs no history."""
    for r in repos:
        r["momentum"] = round(r["stars"] / age_days(r["created_at"], today))
    return repos


def rank_trending(repos, cfg, today, limit):
    repos = drop_noise(list(repos), cfg)
    add_momentum(repos, today)
    key = "momentum" if cfg.get("trending_rank", "momentum") == "momentum" else "stars"
    repos.sort(key=lambda r: r.get(key, 0), reverse=True)
    return repos[:limit]


def gh_repo(full_name, token):
    """Fetch one repo as a search-item-shaped object (for the watchlist)."""
    d = _get_json(REPO_API + full_name, token, full_name, 1, quiet=True)
    return d if d and "id" in d else None


def readme_excerpt(md, n=500):
    """First bit of prose from a README, badges/headings/html stripped."""
    kept = []
    for ln in md.splitlines():
        s = ln.strip()
        # skip blanks, headings, quotes, lists, tables, code fences, badges, html
        if (not s or s[0] in "#>-*|`" or s.startswith("![")
                or s.startswith("[!") or s.startswith("<")):
            continue
        kept.append(s)
        if sum(len(x) for x in kept) > n:
            break
    text = " ".join(" ".join(kept).split()).replace("|", "/")
    return (text[: n - 1] + "…") if len(text) > n else text


def enrich(full_name, token):
    """Extra repo detail for its note. Best-effort; missing pieces stay None."""
    out = {k: None for k in ("pushed_at", "open_issues", "forks",
                             "license", "homepage", "release", "release_date", "readme")}
    d = _get_json(REPO_API + full_name, token, full_name, 1, quiet=True)
    if d:
        out["pushed_at"] = (d.get("pushed_at") or "")[:10] or None
        out["open_issues"] = d.get("open_issues_count")
        out["forks"] = d.get("forks_count")
        lic = (d.get("license") or {}).get("spdx_id")
        out["license"] = lic if lic and lic != "NOASSERTION" else None
        out["homepage"] = (d.get("homepage") or "").strip() or None
    rel = _get_json(REPO_API + full_name + "/releases/latest", token, full_name, 1, quiet=True)
    if rel:
        out["release"] = rel.get("tag_name")
        out["release_date"] = (rel.get("published_at") or "")[:10] or None
    rm = _get_json(REPO_API + full_name + "/readme", token, full_name, 1, quiet=True)
    if rm and rm.get("content"):
        try:
            out["readme"] = readme_excerpt(
                base64.b64decode(rm["content"]).decode("utf-8", "replace"))
        except (ValueError, UnicodeError):
            pass
    return out


def new_today(conn, today, limit, cfg):
    """Repos that entered tracking today (new on radar), top by stars."""
    rows = conn.execute(
        "SELECT r.full_name, r.html_url, r.description, r.created_at, r.language,"
        "       s.stars, r.topics"
        "  FROM repos r JOIN snapshots s ON s.repo_id=r.repo_id AND s.day=?"
        " WHERE r.first_seen=?",
        (today, today),
    ).fetchall()
    cols = ["full_name", "html_url", "description", "created_at", "language", "stars", "topics"]
    repos = drop_noise([dict(zip(cols, row)) for row in rows], cfg)
    add_momentum(repos, today)
    repos.sort(key=lambda r: r["stars"], reverse=True)
    return repos[:limit]


def breakouts(conn, today, ref_day, min_delta, min_age_days, limit, cfg):
    """OLD repos suddenly gaining a lot of stars (created long ago, moving now)."""
    if ref_day is None:
        return []
    rows = conn.execute(
        """SELECT r.full_name, r.html_url, r.description, r.created_at, r.language,
                  t.stars, t.stars - y.stars AS delta, r.topics
           FROM snapshots t
           JOIN snapshots y ON y.repo_id=t.repo_id AND y.day=?
           JOIN repos r ON r.repo_id=t.repo_id
           WHERE t.day=? AND (t.stars - y.stars) >= ?
           ORDER BY delta DESC""",
        (ref_day, today, min_delta),
    ).fetchall()
    cols = ["full_name", "html_url", "description", "created_at", "language", "stars", "delta", "topics"]
    out = []
    for row in rows:
        r = dict(zip(cols, row))
        if is_noise(r, cfg) or age_days(r["created_at"], today) < min_age_days:
            continue
        out.append(r)
        if len(out) >= limit:
            break
    return out


def sparkline_svg(values, w=280, h=38):
    """Tiny inline SVG trend line from a repo's star history (>=2 points)."""
    if len(values) < 2:
        return ""
    lo, hi = min(values), max(values)
    rng = (hi - lo) or 1
    n = len(values)
    pts = [f"{round(i*(w-4)/(n-1)+2,1)},{round(h-3-(v-lo)*(h-6)/rng,1)}"
           for i, v in enumerate(values)]
    lx, ly = pts[-1].split(",")
    return (
        f'<svg width="{w}" height="{h}" viewBox="0 0 {w} {h}" '
        f'xmlns="http://www.w3.org/2000/svg" preserveAspectRatio="none">'
        f'<polyline points="{" ".join(pts)}" fill="none" stroke="#d9a441" '
        f'stroke-width="1.5" stroke-linejoin="round" stroke-linecap="round"/>'
        f'<circle cx="{lx}" cy="{ly}" r="2.4" fill="#d9a441"/></svg>'
    )


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


def trending_table(repos, empty="_No new repos in this window._\n"):
    if not repos:
        return empty
    # uniform 7-col shape so CSS column coloring matches growth tables;
    # col4 carries star/day momentum here (blank stays blank if unknown)
    lines = ["| # | Repo | Stars | ★/day | Lang | Created | What it is |",
             "|--:|------|------:|--:|------|---------|------------|"]
    for i, r in enumerate(repos, 1):
        mom = f"~{r['momentum']}/d" if r.get("momentum") else ""
        lines.append(
            f"| {i} | {wikilink(r['full_name'])} | {r['stars']} | {mom} | "
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


def write_daily_note(base, today, d):
    """d holds the section lists: week, month, g24, g30, new, breakout, watch."""
    daily_dir = os.path.join(base, "Daily")
    os.makedirs(daily_dir, exist_ok=True)
    path = os.path.join(daily_dir, f"{today}.md")
    parts = [
        f"---\ncssclasses: [ai-digest]\ntype: ai-repo-digest\ndate: {today}\n"
        f"tags: [ai-repos, digest]\n---\n\n",
        f"# AI GitHub Repos · {today}\n\n",
        token_warning(today),
    ]
    if d.get("new"):
        parts.append(f"## New on radar\n\n{trending_table(d['new'])}\n")
    if d.get("breakout"):
        parts.append(f"## Breakouts · old repos taking off\n\n{growth_table(d['breakout'])}\n")
    parts.append(f"## Trending · created this week\n\n{trending_table(d['week'])}\n")
    parts.append(f"## Trending · created this month\n\n{trending_table(d['month'])}\n")
    parts.append(f"## Fastest growing · last 24 hours\n\n{growth_table(d['g24'])}\n")
    parts.append(f"## Fastest growing · last 30 days\n\n{growth_table(d['g30'])}\n")
    if d.get("watch"):
        parts.append(f"## Watchlist\n\n{trending_table(d['watch'], empty=EMPTY_WATCH)}\n")
    parts.append(f"---\n*Generated by personal-ai-repo-tracker · "
                 f"{datetime.now().strftime('%Y-%m-%d %H:%M')}*\n")
    with open(path, "w", encoding="utf-8") as f:
        f.write("".join(parts))
    return path


def maybe_claude_summary(daily_path, cfg):
    """If enabled, have the local Claude CLI write a video-idea TL;DR.

    Claude only GENERATES the text (print mode can't reliably edit files);
    Python appends it. Best-effort — never fails the data run.
    """
    if not cfg.get("claude_summary"):
        return
    with open(daily_path, "r", encoding="utf-8") as f:
        note = f.read()
    prompt = (
        "Below is today's AI GitHub repo digest. Write a tight TL;DR for a content "
        "creator: 4-6 markdown bullets naming the most notable repos and why they "
        "matter / what's testable for a video. Output ONLY the bullet lines (start "
        "each with '- '), no preamble, no heading.\n\n" + note
    )
    print("Generating Claude summary...")
    try:
        # prompt via stdin so the long note isn't shell-escaped; shell=True resolves claude.cmd
        res = subprocess.run("claude -p", shell=True, input=prompt,
                             capture_output=True, text=True,
                             encoding="utf-8", errors="replace", timeout=300)
        out = (res.stdout or "").strip()
        if out:
            with open(daily_path, "a", encoding="utf-8") as f:
                f.write("\n## TL;DR — video ideas\n\n" + out + "\n")
            print("  appended TL;DR")
        else:
            print(f"  no summary returned (claude unavailable?); skipped. {res.stderr[:120]}")
    except Exception as e:  # ponytail: nice-to-have, never break the data run over it
        print(f"  summary step skipped: {e}")


def write_repo_note(conn, base, r, today, extra=None, sparkline=True):
    repo_dir = os.path.join(base, "Repos")
    os.makedirs(repo_dir, exist_ok=True)
    full_name = r["full_name"]
    path = os.path.join(repo_dir, f"{safe_name(full_name)}.md")

    hist = conn.execute(
        "SELECT day, stars FROM snapshots WHERE repo_id="
        "(SELECT repo_id FROM repos WHERE full_name=?) ORDER BY day DESC LIMIT 30",
        (full_name,),
    ).fetchall()
    hist_tbl = "| Day | Stars |\n|-----|------:|\n" + "\n".join(
        f"| {d} | {s} |" for d, s in hist[:14]
    )
    spark = sparkline_svg([s for _, s in reversed(hist)]) if sparkline else ""
    spark_block = f"{spark}\n\n" if spark else ""

    topics = _topics_list(r)

    # enrichment stat line + release + homepage + readme (all optional)
    meta_bits = []
    if extra:
        if extra.get("pushed_at"):
            meta_bits.append(f"last push {extra['pushed_at']}")
        if extra.get("open_issues") is not None:
            meta_bits.append(f"{extra['open_issues']} open issues")
        if extra.get("forks") is not None:
            meta_bits.append(f"{extra['forks']} forks")
        if extra.get("license"):
            meta_bits.append(extra["license"])
    meta_line = ("  ·  ".join(meta_bits) + "\n\n") if meta_bits else ""
    rel_line = ""
    if extra and extra.get("release"):
        rd = f" · {extra['release_date']}" if extra.get("release_date") else ""
        rel_line = f"Latest release — **{extra['release']}**{rd}\n\n"
    links = f"[Open on GitHub →]({r['html_url']})"
    if extra and extra.get("homepage"):
        links += f" · [Homepage →]({extra['homepage']})"
    links += "\n\n"
    readme_block = (f"## README\n\n> {extra['readme']}\n\n"
                    if extra and extra.get("readme") else "")

    body = (
        f"---\ncssclasses: [ai-repo]\nrepo: {full_name}\nurl: {r['html_url']}\n"
        f"stars: {r.get('stars', '')}\n"
        f"created: {r['created_at']}\nlanguage: {r['language']}\n"
        f"topics: [{', '.join(topics)}]\nupdated: {today}\ntags: [ai-repo]\n---\n\n"
        f"# {full_name}\n\n"
        f"> {trunc(r['description'], 400) or 'no description'}\n\n"
        f"**★ {r.get('stars', '?')}**  ·  {r['language'] or '—'}  ·  created {r['created_at']}\n\n"
        + meta_line
        + rel_line
        + links
        + (f"Topics — {' · '.join(topics)}\n\n" if topics else "")
        + readme_block
        + f"## Star history\n\n{spark_block}{hist_tbl}\n"
    )
    with open(path, "w", encoding="utf-8") as f:
        f.write(body)


def week_key(today):
    iso = datetime.strptime(today, "%Y-%m-%d").date().isocalendar()
    return f"{iso[0]}-W{iso[1]:02d}"


def write_weekly_note(base, conn, today, cfg):
    """Rolling week-in-review: biggest 7-day gainers + this week's new entrants."""
    week_dir = os.path.join(base, "Weekly")
    os.makedirs(week_dir, exist_ok=True)
    key = week_key(today)
    ref7 = reference_day(conn, today, 7)
    gainers = drop_noise(growth(conn, today, ref7, 30), cfg, 15)

    wk = (datetime.strptime(today, "%Y-%m-%d").date() - timedelta(days=7)).isoformat()
    rows = conn.execute(
        "SELECT r.full_name, r.html_url, r.description, r.created_at, r.language,"
        "       s.stars, r.topics"
        "  FROM repos r JOIN snapshots s ON s.repo_id=r.repo_id AND s.day=?"
        " WHERE r.first_seen>=? ORDER BY s.stars DESC LIMIT 30",
        (today, wk),
    ).fetchall()
    cols = ["full_name", "html_url", "description", "created_at", "language", "stars", "topics"]
    entrants = drop_noise([dict(zip(cols, row)) for row in rows], cfg, 15)
    add_momentum(entrants, today)

    body = (
        f"---\ncssclasses: [ai-digest]\ntype: ai-weekly\nweek: {key}\n"
        f"tags: [ai-repos, weekly]\n---\n\n"
        f"# AI Repos · week {key}\n\n"
        f"## Biggest gainers this week\n\n{growth_table(gainers)}\n"
        f"## New entrants this week\n\n{trending_table(entrants)}\n"
        f"---\n*Rebuilt each run through the week · {today}*\n"
    )
    path = os.path.join(week_dir, f"{key}.md")
    with open(path, "w", encoding="utf-8") as f:
        f.write(body)
    return path


def write_home_note(base, conn, today, watch, cfg):
    """A map-of-content home note: latest links + top tracked + recent days."""
    rows = conn.execute(
        "SELECT full_name, html_url, description, created_at, language, last_stars, topics"
        "  FROM repos ORDER BY last_stars DESC LIMIT 30"
    ).fetchall()
    cols = ["full_name", "html_url", "description", "created_at", "language", "stars", "topics"]
    top = drop_noise([dict(zip(cols, row)) for row in rows], cfg, 15)
    add_momentum(top, today)
    days = [d for (d,) in conn.execute(
        "SELECT DISTINCT day FROM snapshots ORDER BY day DESC LIMIT 14").fetchall()]
    daily_links = "\n".join(f"- [[{d}]]" for d in days) or "_none yet_"

    parts = [
        f"---\ncssclasses: [ai-digest]\ntype: ai-home\ntags: [ai-repos, home]\n---\n\n",
        f"# AI Repos · Home\n\n",
        f"Latest digest: [[{today}]]  ·  This week: [[{week_key(today)}]]\n\n",
        f"## Top tracked · all-time by stars\n\n{trending_table(top)}\n",
    ]
    if watch:
        parts.append(f"## Watchlist\n\n{trending_table(watch, empty=EMPTY_WATCH)}\n")
    parts.append(f"## Recent daily digests\n\n{daily_links}\n\n")
    parts.append(f"---\n*Updated {today}*\n")
    path = os.path.join(base, "Home.md")
    with open(path, "w", encoding="utf-8") as f:
        f.write("".join(parts))
    return path


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

    week = rank_trending(merge_by_id(week_lists).values(), cfg, today, cfg["trending_week_count"])
    month = rank_trending(merge_by_id(month_lists).values(), cfg, today, cfg["trending_month_count"])

    # watchlist repos, fetched individually so they're always tracked
    watch_items = []
    for fn in cfg.get("watchlist", []):
        obj = gh_repo(fn, token)
        if obj:
            watch_items.append(obj)
        else:
            print(f"  watchlist: could not fetch {fn}")

    # 2) snapshot universe + trending + watchlist so growth can be diffed over time
    universe = merge_by_id(universe_lists + week_lists + month_lists + [watch_items])
    print(f"Snapshotting {len(universe)} repos for {today}...")
    snapshot(conn, universe, today)

    # 3) growth + discovery from snapshot history (noise-filtered)
    ref24 = reference_day(conn, today, 1)
    ref30 = reference_day(conn, today, 30)
    g24 = drop_noise(growth(conn, today, ref24, cfg["growth_24h_count"] * 3), cfg, cfg["growth_24h_count"])
    g30 = drop_noise(growth(conn, today, ref30, cfg["growth_30d_count"] * 3), cfg, cfg["growth_30d_count"])
    new = new_today(conn, today, cfg.get("new_on_radar_count", 10), cfg)
    brk = breakouts(conn, today, ref24, cfg.get("breakout_min_delta", 200),
                    cfg.get("breakout_min_age_days", 45), cfg.get("breakout_count", 10), cfg)
    watch = add_momentum([normalize(o) for o in watch_items], today)
    print(f"  24h ref: {ref24} ({len(g24)}) | 30d ref: {ref30} ({len(g30)}) | "
          f"new: {len(new)} | breakouts: {len(brk)} | watch: {len(watch)}")

    # 4) dashboard + weekly rollup + home MOC
    if token_warning(today):
        print("  WARNING: " + token_warning(today).splitlines()[0][13:])
    data = {"week": week, "month": month, "g24": g24, "g30": g30,
            "new": new, "breakout": brk, "watch": watch}
    daily_path = write_daily_note(base, today, data)
    if cfg.get("weekly_note", True):
        write_weekly_note(base, conn, today, cfg)
    if cfg.get("home_note", True):
        write_home_note(base, conn, today, watch, cfg)

    # 5) one enriched note per surfaced repo
    surfaced = {}
    for r in (new + brk + week + month + g24 + g30 + watch):
        surfaced.setdefault(r["full_name"], r)
    enrich_on = cfg.get("enrich_repo_notes", True)
    spark_on = cfg.get("sparklines", True)
    if enrich_on:
        print(f"Enriching {len(surfaced)} repo notes...")
    for r in surfaced.values():
        extra = enrich(r["full_name"], token) if enrich_on else None
        write_repo_note(conn, base, r, today, extra, spark_on)

    conn.close()
    maybe_claude_summary(daily_path, cfg)
    print(f"\nDone. {len(surfaced)} repo notes + dashboard + weekly + home:\n  {daily_path}")


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

    # signal helpers
    assert is_noise({"full_name": "x/awesome-ai", "topics": [], "description": ""}, {})
    assert not is_noise({"full_name": "x/tool", "topics": ["ai"], "description": "a tool"}, {})
    mom = add_momentum([{"full_name": "a/b", "stars": 300, "created_at": "2026-06-27"}], "2026-06-30")
    assert mom[0]["momentum"] == 100, mom                 # 300 stars / 3 days
    assert sparkline_svg([1, 5, 3]).startswith("<svg") and sparkline_svg([1]) == ""
    # token expiry warning: silent when far off or no expiry, loud inside the window
    assert token_warning("2026-11-10", None) == ""
    assert token_warning("2026-11-10", "2026-12-25 09:19:36 UTC") == ""
    assert "expires in 5 day(s)" in token_warning("2026-11-10", "2026-11-15 09:19:36 UTC")
    print("selftest OK")


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        selftest()
    else:
        run()
