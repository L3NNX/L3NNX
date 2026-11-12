#!/usr/bin/env python3
"""Refresh data/contributions.json from the GitHub GraphQL API (stdlib only).

Used by the daily Action. Needs GH_TOKEN (or GITHUB_TOKEN). The default Action
token only sees public contributions; add a classic PAT with `read:user` as the
PROFILE_TOKEN secret to include private ones.
"""
import datetime as dt
import json
import os
import sys
import urllib.request
from pathlib import Path

USER = "L3NNX"
FEATURED = ["LUXE", "CordBin", "Ephem"]  # owner is USER; order = order of the cards
OUT = Path(__file__).resolve().parent.parent / "data" / "contributions.json"

REPO_FIELDS = """name description url stargazerCount forkCount
  primaryLanguage { name color }"""


def gql(token, query, variables=None):
    req = urllib.request.Request(
        "https://api.github.com/graphql",
        data=json.dumps({"query": query, "variables": variables or {}}).encode(),
        headers={"Authorization": f"bearer {token}", "Content-Type": "application/json",
                 "User-Agent": "profile-cards"})
    with urllib.request.urlopen(req, timeout=30) as r:
        body = json.load(r)
    if body.get("errors") and not body.get("data"):
        raise SystemExit(f"GraphQL error: {body['errors']}")
    return body


def main():
    token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
    if not token:
        raise SystemExit("Set GH_TOKEN or GITHUB_TOKEN")
    now = dt.datetime.now(dt.timezone.utc)
    start = (now - dt.timedelta(days=364)).replace(hour=0, minute=0, second=0, microsecond=0)

    repo_q = "".join(
        f'r{i}: repository(owner: "{USER}", name: "{name}") {{ {REPO_FIELDS} }}\n'
        for i, name in enumerate(FEATURED))
    query = f"""
    query($login: String!, $from: DateTime!, $to: DateTime!) {{
      user(login: $login) {{
        contributionsCollection(from: $from, to: $to) {{
          contributionCalendar {{ totalContributions weeks {{ contributionDays {{ date contributionCount }} }} }}
        }}
      }}
      {repo_q}
    }}"""
    body = gql(token, query, {"login": USER, "from": start.isoformat(), "to": now.isoformat()})
    data = body["data"]
    cal = data["user"]["contributionsCollection"]["contributionCalendar"]
    days = [{"date": d["date"], "count": d["contributionCount"]}
            for w in cal["weeks"] for d in w["contributionDays"]]
    repos = []
    for i, name in enumerate(FEATURED):
        r = data.get(f"r{i}")
        if not r:
            print(f"warning: repo {USER}/{name} not found", file=sys.stderr)
            repos.append({"name": name, "owner": USER})
            continue
        lang = r.get("primaryLanguage") or {}
        repos.append({"name": r["name"], "owner": USER, "description": r.get("description"),
                      "url": r["url"], "stars": r["stargazerCount"], "forks": r["forkCount"],
                      "language": lang.get("name"), "language_color": lang.get("color")})

    OUT.write_text(json.dumps({
        "user": USER, "generated": now.isoformat(timespec="seconds"),
        "total": cal["totalContributions"], "days": days, "repos": repos}, indent=1) + "\n")
    print(f"wrote {OUT} ({len(days)} days, {cal['totalContributions']} contributions, {len(repos)} repos)")


if __name__ == "__main__":
    main()
