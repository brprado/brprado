"""
Renders dark_mode.svg and light_mode.svg: a neofetch-style profile card with live GitHub stats.

Runs every day through .github/workflows/build.yaml. To change what the card says, edit the
PROFILE list below and push; the dot leaders realign on their own.

Env vars:
  ACCESS_TOKEN  GitHub token used for the stats. Without it the card is still rendered
                (uptime refreshes) using the last stats saved in cache/stats.json.
  USER_NAME     GitHub login (default: brprado).
"""
import datetime as dt
import hashlib
import json
import os
import sys
import time
from html import escape
from pathlib import Path
from zoneinfo import ZoneInfo

import requests
from dateutil.relativedelta import relativedelta

ROOT = Path(__file__).resolve().parent
USER = os.environ.get("USER_NAME", "brprado")
TOKEN = os.environ.get("ACCESS_TOKEN", "").strip()
TIMEZONE = ZoneInfo("America/Bahia")
UPTIME_SINCE = dt.date(2021, 8, 12)  # date the uptime counts from
CACHE = ROOT / "cache" / "stats.json"
# Repo filters, stored as sha256("owner/name") so private repo names don't show up in this
# public file. To add one:  printf '%s' 'owner/name' | shasum -a 256
EXCLUDE_FROM_LOC = {  # lines are left out, commits still count
    "a1b81cf8cd55c11bd78f63090f7b18293d76a0600355ec8118ecc07c466c182b",  # private notes repo
}
EXCLUDE_ENTIRELY = {  # neither commits nor lines count (automation / bot-generated history)
    "af93c95a091298199eca9fb1ab691140e69ad762b30d1fc605ff30cb54195a6b",  # automation: backups
    "6f7c9ea784aaee898f5d2f87cccf974764010994a3bb29d4c9afaa510280a37b",  # automation: CI/CD sync
    "73c6e3a77b5c9e0dc35089364adf883ecae898808afc69852106e301f071fe1f",  # automation: workflow sync
}
# Commits touching more lines than this are almost always data dumps, backups or generated
# files (CSVs, notebook outputs, lockfiles). They still count as commits, but not as lines.
MAX_LINES_PER_COMMIT = 5_000
CACHE_VERSION = 2  # bump when the counting rules above change, to force a full rescan

# ── What the card says ──────────────────────────────────────────────────────────────────
# ("title", text) | ("kv", "Key.Sub", value) | ("blank",) | ("gap",) | ("section", text) | ("stats",)
PROFILE = [
    ("title", "bruno@prado"),
    ("kv", "OS", "macOS, Linux"),
    ("kv", "Uptime", "{uptime}"),
    ("kv", "Host", "AB InBev"),
    ("kv", "Kernel", "Data Engineer"),
    ("kv", "IDE", "VSCode, Jupyter, Claude Code"),
    ("blank",),
    ("kv", "Languages.Programming", "Python, SQL, PySpark"),
    ("kv", "Languages.Config", "YAML, HCL, JSON"),
    ("kv", "Languages.Real", "Portuguese, English"),
    ("blank",),
    ("kv", "Data.Processing", "Spark, Databricks, Kafka"),
    ("kv", "Data.Orchestration", "Airflow, Azure Data Factory"),
    ("kv", "Data.Transformation", "dbt, Spark SQL"),
    ("kv", "Data.Storage", "Delta Lake, ADLS Gen2, Synapse"),
    ("kv", "Data.Architecture", "Lakehouse, Medallion, ETL/ELT"),
    ("kv", "Data.Modeling", "Star Schema, SCD Type 2"),
    ("kv", "Cloud.Infra", "Azure, Terraform, CI/CD"),
    ("kv", "AI.Stack", "LangChain, CrewAI, LLMs, RAG"),
    ("gap",),
    ("section", "Contact"),
    ("kv", "Email.Personal", "brunopradocode@gmail.com"),
    ("kv", "LinkedIn", "bruno-prado-7b5b6b1a3"),
    ("gap",),
    ("section", "GitHub Stats"),
    ("stats",),
]

# ── Look ────────────────────────────────────────────────────────────────────────────────
WIDTH = 60            # characters per info line
INFO_X, ART_X = 390, 15
TOP, LINE_H = 30, 20
ART_FONT, ART_LINE_H = 10, 11  # the portrait uses a smaller font for more detail; it is centered vertically
THEMES = {
    "dark": dict(bg="#161b22", fg="#c9d1d9", key="#ffa657", value="#a5d6ff",
                 add="#3fb950", dele="#f85149", cc="#616e7f"),
    "light": dict(bg="#f6f8fa", fg="#24292f", key="#953800", value="#0a3069",
                  add="#1a7f37", dele="#cf222e", cc="#c2cfde"),
}

# ── GitHub GraphQL ──────────────────────────────────────────────────────────────────────
API = "https://api.github.com/graphql"
SESSION = requests.Session()
CALLS = 0

Q_USER = """query($login: String!) { user(login: $login) { id followers { totalCount } } }"""

Q_REPOS = """
query($login: String!, $aff: [RepositoryAffiliation], $cursor: String) {
  user(login: $login) {
    repositories(first: 50, after: $cursor, ownerAffiliations: $aff) {
      totalCount
      pageInfo { hasNextPage endCursor }
      nodes {
        nameWithOwner
        stargazerCount
        defaultBranchRef { target { oid } }
      }
    }
  }
}"""

Q_HISTORY = """
query($owner: String!, $name: String!, $uid: ID!, $cursor: String) {
  repository(owner: $owner, name: $name) {
    defaultBranchRef { target { ... on Commit {
      history(first: 100, after: $cursor, author: {id: $uid}) {
        pageInfo { hasNextPage endCursor }
        nodes { additions deletions }
      }
    } } }
  }
}"""


def gql(query, **variables):
    global CALLS
    for attempt in range(4):
        CALLS += 1
        r = SESSION.post(API, json={"query": query, "variables": variables}, timeout=90)
        if r.status_code == 200:
            body = r.json()
            if body.get("data") is not None:
                return body["data"]
            raise RuntimeError(f"GraphQL error: {body.get('errors')}")
        if r.status_code in (403, 429, 502, 503, 504):  # rate/abuse limit or timeout: back off
            time.sleep(int(r.headers.get("retry-after", 10 * (attempt + 1))))
            continue
        r.raise_for_status()
    raise RuntimeError(f"GitHub API kept failing ({r.status_code}): {r.text[:200]}")


def list_repos(affiliations):
    repos, cursor = [], None
    while True:
        conn = gql(Q_REPOS, login=USER, aff=affiliations, cursor=cursor)["user"]["repositories"]
        repos += conn["nodes"]
        if not conn["pageInfo"]["hasNextPage"]:
            return repos
        cursor = conn["pageInfo"]["endCursor"]


def my_commits(name_with_owner, uid):
    """Commits authored by me on the default branch, with their added/deleted lines."""
    owner, name = name_with_owner.split("/", 1)
    commits = additions = deletions = 0
    cursor = None
    while True:
        repo = gql(Q_HISTORY, owner=owner, name=name, uid=uid, cursor=cursor)["repository"]
        if not repo or not repo["defaultBranchRef"]:
            break
        history = repo["defaultBranchRef"]["target"]["history"]
        for c in history["nodes"]:
            commits += 1
            if c["additions"] + c["deletions"] <= MAX_LINES_PER_COMMIT:
                additions += c["additions"]
                deletions += c["deletions"]
        if not history["pageInfo"]["hasNextPage"]:
            break
        cursor = history["pageInfo"]["endCursor"]
    return {"commits": commits, "add": additions, "del": deletions}


def fetch_stats(cache):
    """Fetch fresh stats. Per-repo line counts are cached by default-branch HEAD so only
    repos with new commits are re-scanned. Repo names are stored hashed, since private
    repos are included and this cache lives in a public repo."""
    SESSION.headers["Authorization"] = f"bearer {TOKEN}"
    user = gql(Q_USER, login=USER)["user"]
    owned = list_repos(["OWNER"])
    everything = list_repos(["OWNER", "COLLABORATOR", "ORGANIZATION_MEMBER"])

    old = cache.get("repos", {}) if cache.get("version") == CACHE_VERSION else {}
    repos = {}
    for r in everything:
        key = hashlib.sha256(r["nameWithOwner"].encode()).hexdigest()
        head = (r["defaultBranchRef"] or {}).get("target", {}).get("oid")
        if not head or key in EXCLUDE_ENTIRELY:
            continue
        if key in old and old[key]["head"] == head:
            repos[key] = old[key]
        else:
            repos[key] = {"head": head, **my_commits(r["nameWithOwner"], user["id"])}

    counted = [v for k, v in repos.items() if k not in EXCLUDE_FROM_LOC]
    return {
        "repos_owned": len(owned),
        "repos_contributed": len(everything),
        "stars": sum(r["stargazerCount"] for r in owned),
        "followers": user["followers"]["totalCount"],
        "commits": sum(v["commits"] for v in repos.values()),
        "loc_add": sum(v["add"] for v in counted),
        "loc_del": sum(v["del"] for v in counted),
    }, repos


# ── Rendering ───────────────────────────────────────────────────────────────────────────
def uptime(today):
    d = relativedelta(today, UPTIME_SINCE)
    plural = lambda n, unit: f"{n} {unit}{'' if n == 1 else 's'}"
    cake = " 🎂" if (d.months, d.days) == (0, 0) else ""
    return f"{plural(d.years, 'year')}, {plural(d.months, 'month')}, {plural(d.days, 'day')}{cake}"


def span(text, cls=None):
    return f'<tspan class="{cls}">{escape(text)}</tspan>' if cls else escape(text)


def dots(n):
    """Dot leader that fills n characters (always at least one space)."""
    return " " + "." * (n - 2) + " " if n >= 3 else " " * max(n, 1)


def field(key, value_parts, width):
    """'Key.Sub: ..... value' padded to `width` chars. value_parts = [(text, cls), ...]."""
    keys = key.split(".")
    key_svg = ".".join(span(k, "key") for k in keys)
    used = len(key) + 1 + sum(len(t) for t, _ in value_parts)
    value_svg = "".join(span(t, c) for t, c in value_parts)
    return f'{key_svg}:{span(dots(width - used), "cc")}{value_svg}'


def fmt(n):
    return f"{n:,}"


def stats_lines(s):
    left = WIDTH // 2 + 3  # column where " | " starts
    right = WIDTH - left - 3
    line1 = (field("Repos", [(fmt(s["repos_owned"]), "value"), (" {", None), ("Contributed", "key"),
                             (": ", None), (fmt(s["repos_contributed"]), "value"), ("}", None)], left - 2)
             + " | " + field("Stars", [(fmt(s["stars"]), "value")], right))
    line2 = (field("Commits", [(fmt(s["commits"]), "value")], left - 2)
             + " | " + field("Followers", [(fmt(s["followers"]), "value")], right))
    line3 = field("Lines of Code on GitHub", [
        (fmt(s["loc_add"] - s["loc_del"]), "value"), (" ( ", None),
        (fmt(s["loc_add"]), "add"), ("++", "add"), (", ", None),
        (fmt(s["loc_del"]), "dele"), ("--", "dele"), (" )", None)], WIDTH - 2)
    return [line1, line2, line3]


def info_lines(stats, today):
    out = []
    for item in PROFILE:
        kind = item[0]
        if kind == "title":
            out.append(span(item[1]) + " " + "─" * (WIDTH - len(item[1]) - 1))
        elif kind == "section":
            label = f"- {item[1]} "
            out.append(span(label) + "─" * (WIDTH - len(label)))
        elif kind == "kv":
            value = item[2].replace("{uptime}", uptime(today))
            out.append(span(". ", "cc") + field(item[1], [(value, "value")], WIDTH - 2))
        elif kind == "blank":
            out.append(span(".", "cc"))
        elif kind == "gap":
            out.append("")
        elif kind == "stats":
            out += [span(". ", "cc") + line for line in stats_lines(stats)]
    return out


def render(theme, art, info):
    c = THEMES[theme]
    height = max(TOP + LINE_H * (len(info) - 1) + 20, ART_LINE_H * len(art) + 30)
    art_top = (height - ART_LINE_H * len(art)) / 2 + ART_FONT * 0.8  # baseline of the first art row
    width = 985
    svg = [
        "<?xml version='1.0' encoding='UTF-8'?>",
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}px" height="{height}px" '
        'font-family="ConsolasFallback,Consolas,Menlo,\'DejaVu Sans Mono\',monospace" font-size="16px">',
        "<style>",
        "@font-face { src: local('Consolas'), local('Consolas Bold'); font-family: 'ConsolasFallback';"
        " font-display: swap; size-adjust: 109%; }",
        f".key {{fill: {c['key']};}} .value {{fill: {c['value']};}} .cc {{fill: {c['cc']};}}",
        f".add {{fill: {c['add']};}} .dele {{fill: {c['dele']};}}",
        "text, tspan {white-space: pre;}",
        "</style>",
        f'<rect width="{width}px" height="{height}px" fill="{c["bg"]}" rx="15"/>',
        f'<text fill="{c["fg"]}" font-size="{ART_FONT}px">',
    ]
    svg += [f'<tspan x="{ART_X}" y="{art_top + ART_LINE_H * i:.1f}">{escape(line)}</tspan>'
            for i, line in enumerate(art)]
    svg.append("</text>")
    svg.append(f'<text fill="{c["fg"]}">')
    svg += [f'<tspan x="{INFO_X}" y="{TOP + LINE_H * i}">{line}</tspan>' for i, line in enumerate(info) if line]
    svg += ["</text>", "</svg>", ""]
    (ROOT / f"{theme}_mode.svg").write_text("\n".join(svg), encoding="utf-8")


def main():
    start = time.perf_counter()
    cache = json.loads(CACHE.read_text()) if CACHE.exists() else {}
    if TOKEN:
        stats, repos = fetch_stats(cache)
        CACHE.parent.mkdir(exist_ok=True)
        CACHE.write_text(json.dumps({"version": CACHE_VERSION, "stats": stats, "repos": repos},
                                    indent=1, sort_keys=True) + "\n")
    elif "stats" in cache:
        print("::warning::ACCESS_TOKEN not set; rendering with the last cached stats.")
        stats = cache["stats"]
    else:
        sys.exit("ACCESS_TOKEN is not set and there are no cached stats to fall back on.")

    today = dt.datetime.now(TIMEZONE).date()
    info = info_lines(stats, today)
    art = (ROOT / "art" / "portrait.txt").read_text(encoding="utf-8").splitlines()
    for theme in THEMES:
        render(theme, art, info)

    print(json.dumps(stats, indent=2))
    print(f"GraphQL calls: {CALLS} | {time.perf_counter() - start:.1f}s")


if __name__ == "__main__":
    main()
