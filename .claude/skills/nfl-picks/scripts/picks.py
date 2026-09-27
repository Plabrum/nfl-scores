#!/usr/bin/env python3
"""Helpers for filling out a Google Forms NFL pick 'em against live odds.

Subcommands:
  form URL                      questions, entry ids and options as JSON
  lines --week N                ESPN/DraftKings spread and total per game
  injuries --week N             non-active injuries per game (ESPN, ~5 per team)
  compare URL --week N          form spread vs market spread for each game question
  prefill URL --answers FILE    prefilled viewform link from a JSON answers file

The answers file maps entry ids (or exact question titles) to answer text,
plus an optional "emailAddress" key:
  {"emailAddress": "me@example.com", "1498737440": "Phil", "1743186246": "LA Chargers +7.5"}
"""
import argparse
import json
import re
import sys
import urllib.parse
import urllib.request

ESPN = "https://site.api.espn.com/apis/site/v2/sports/football/nfl"


def get(url):
    return urllib.request.urlopen(url).read().decode()


def base_url(url):
    m = re.search(r"(https://docs\.google\.com/forms/d/e/[^/]+/)", url)
    if not m:
        sys.exit("Expected a https://docs.google.com/forms/d/e/<id>/viewform link")
    return m.group(1)


def load_form(url):
    html = get(base_url(url) + "viewform")
    m = re.search(r"FB_PUBLIC_LOAD_DATA_ = (.*?);</script>", html, re.S)
    if not m:
        sys.exit("Could not find form data; the form may require sign-in.")
    d = json.loads(m.group(1))
    questions = []
    for it in d[1][1] or []:
        if not it[4]:
            continue  # section headers, images, etc.
        q = it[4][0]
        questions.append({
            "id": str(q[0]),
            "title": it[1],
            "type": it[3],  # 0 short text, 1 paragraph, 2 multiple choice, 4 checkbox
            "required": bool(q[2]) if len(q) > 2 else False,
            "options": [o[0] for o in (q[1] or [])],
        })
    return {
        "title": d[1][8] if len(d[1]) > 8 else d[3],
        "description": d[1][0],
        "collects_email": 'type="email"' in html,
        "questions": questions,
    }


def scoreboard(week, season=None, seasontype=2):
    q = f"seasontype={seasontype}&week={week}" + (f"&dates={season}" if season else "")
    return json.loads(get(f"{ESPN}/scoreboard?{q}"))["events"]


def game_lines(week, season=None):
    games = []
    for e in scoreboard(week, season):
        c = e["competitions"][0]
        odds = (c.get("odds") or [{}])[0]
        teams = {t["homeAway"]: t["team"] for t in c["competitors"]}
        games.append({
            "id": e["id"],
            "matchup": e["shortName"],
            "kickoff": c["status"]["type"]["shortDetail"],
            "final": c["status"]["type"]["completed"],
            "away": teams["away"],
            "home": teams["home"],
            "provider": odds.get("provider", {}).get("name"),
            "spread": odds.get("details"),  # e.g. "BUF -7"
            "total": odds.get("overUnder"),
        })
    return games


def match_team(text, teams):
    """Find which ESPN team a form label like 'LA Chargers' or 'Kansas City' means."""
    t = text.lower()
    for team in teams:
        if team["name"].lower() in t:  # nickname: Chargers, Giants, 49ers
            return team
    for team in teams:
        if team["location"].lower() in t:  # city: Kansas City, New Orleans
            return team
    return None


def parse_option(opt):
    """'Buffalo -7.5' -> ('Buffalo', -7.5); 'Miami +11.5' -> ('Miami', 11.5)."""
    m = re.match(r"(.+?)\s*([+-−]\s*\d+(?:\.\d+)?)\s*$", opt)
    if not m:
        return opt.strip(), None
    return m.group(1).strip(), float(m.group(2).replace("−", "-").replace(" ", ""))


def market_spread_for(team, game):
    """Market spread from `team`'s point of view (negative = favored)."""
    if not game["spread"]:
        return None
    if game["spread"].upper() == "EVEN":
        return 0.0
    abbr, line = game["spread"].rsplit(" ", 1)
    line = float(line)
    return line if abbr == team["abbreviation"] else -line


def cmd_form(a):
    print(json.dumps(load_form(a.url), indent=2))


def cmd_lines(a):
    for g in game_lines(a.week, a.season):
        if g["final"]:
            print(f"{g['matchup']:<12} FINAL")
            continue
        print(f"{g['matchup']:<12} {g['kickoff']:<22} {g['spread'] or '-':<10} O/U {g['total'] or '-'}  ({g['provider']})")


def cmd_injuries(a):
    for g in game_lines(a.week, a.season):
        if g["final"]:
            continue
        s = json.loads(get(f"{ESPN}/summary?event={g['id']}"))
        print(f"===== {g['matchup']}")
        for t in s.get("injuries", []):
            rows = []
            for i in t.get("injuries", []):
                status = i.get("status") or i.get("type", {}).get("description")
                if not status or status.lower() == "active":
                    continue
                ath = i.get("athlete", {})
                pos = ath.get("position", {}).get("abbreviation", "")
                detail = i.get("details", {}).get("type", "")
                rows.append(f"{ath.get('displayName')} ({pos}) {status}" + (f" - {detail}" if detail else ""))
            print(f"  {t['team']['abbreviation']}: " + ("; ".join(rows) or "none listed"))


def cmd_compare(a):
    form = load_form(a.url)
    games = [g for g in game_lines(a.week, a.season) if not g["final"]]
    used = set()
    for q in form["questions"]:
        if q["type"] != 2 or len(q["options"]) != 2:
            continue
        parsed = [parse_option(o) for o in q["options"]]
        if any(line is None for _, line in parsed):
            continue
        g = next((g for g in games
                  if match_team(parsed[0][0], [g["away"], g["home"]])
                  and match_team(parsed[1][0], [g["away"], g["home"]])), None)
        header = q["title"].split("\n")[0]
        if not g:
            print(f"?? {header}: no matching ESPN game")
            continue
        used.add(g["id"])
        print(f"{header}   [{g['kickoff']}, O/U {g['total']}]")
        for (name, form_line), opt in zip(parsed, q["options"]):
            team = match_team(name, [g["away"], g["home"]])
            mkt = market_spread_for(team, g)
            edge = None if mkt is None else form_line - mkt  # >0 means form gives more points than market
            tag = "" if not edge else (f"  <-- +{edge:g} vs market" if edge > 0 else f"  ({edge:g} vs market)")
            print(f"    {opt:<22} market {'' if mkt is None else f'{mkt:+g}'}{tag}")
        print(f"    entry id: {q['id']}")
    for g in games:
        if g["id"] not in used:
            print(f"!! ESPN game not on form: {g['matchup']} {g['kickoff']}")


def cmd_prefill(a):
    form = load_form(a.url)
    answers = json.load(open(a.answers))
    by_title = {q["title"]: q for q in form["questions"]}
    by_id = {q["id"]: q for q in form["questions"]}
    params = [("usp", "pp_url")]
    if "emailAddress" in answers:
        params.append(("emailAddress", answers.pop("emailAddress")))
    problems = []
    for key, val in answers.items():
        q = by_id.get(str(key)) or by_title.get(key)
        if not q:
            problems.append(f"unknown question: {key}")
            continue
        if q["options"] and val not in q["options"]:
            problems.append(f"{q['title'][:40]!r}: {val!r} not in {q['options']}")
        params.append((f"entry.{q['id']}", str(val)))
    answered = {p[0] for p in params}
    for q in form["questions"]:
        if q["required"] and f"entry.{q['id']}" not in answered:
            problems.append(f"missing required: {q['title'][:60]!r}")
    if form["collects_email"] and "emailAddress" not in answered:
        problems.append("form collects email but no emailAddress given")
    for p in problems:
        print("WARNING:", p, file=sys.stderr)
    print(base_url(a.url) + "viewform?" + urllib.parse.urlencode(params, quote_via=urllib.parse.quote))


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    for name, fn, needs_url, needs_week in [
        ("form", cmd_form, True, False),
        ("lines", cmd_lines, False, True),
        ("injuries", cmd_injuries, False, True),
        ("compare", cmd_compare, True, True),
        ("prefill", cmd_prefill, True, False),
    ]:
        sp = sub.add_parser(name)
        sp.set_defaults(fn=fn)
        if needs_url:
            sp.add_argument("url")
        if needs_week:
            sp.add_argument("--week", type=int, required=True)
            sp.add_argument("--season", type=int, help="season start year, e.g. 2026")
        if name == "prefill":
            sp.add_argument("--answers", required=True)
    a = p.parse_args()
    a.fn(a)


if __name__ == "__main__":
    main()
