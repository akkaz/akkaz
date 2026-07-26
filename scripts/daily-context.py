#!/usr/bin/env python3
"""Contesto per la "frase del giorno" del profilo akkaz.

Stampa un JSON con i messaggi di commit delle ultime ore e qualche numero
sulla nottata. Non stampa MAI nomi di repository, organizzazioni o branch:
la genericizzazione parte dai dati, non dalle buone intenzioni di chi legge.

Uso:  python3 scripts/daily-context.py
"""

import json
import os
import re
import subprocess
import urllib.request
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

ROME = ZoneInfo("Europe/Rome")
WINDOW_HOURS = 36          # copre "ieri" anche quando la routine gira tardi
STALE_DAYS = 7             # rete di sicurezza per i giorni a vuoto
PROFILE_REPO = "akkaz/akkaz"
MAX_COMMITS = 20
MAX_COMPARE = 25           # tetto alle chiamate: senza token si viene bloccati a 60/h

# commit che non raccontano niente di interessante
BORING = re.compile(
    r"^(chore|merge\b|revert\b|bump\b|wip\b|fixup\b|initial commit)|frase del giorno",
    re.IGNORECASE,
)


def gh(path):
    """Chiama l'API GitHub: prima via gh CLI (vede i repo privati), poi HTTP.

    L'ambiente della routine potrebbe non avere gh installato o autenticato,
    quindi il fallback HTTP non e' opzionale: senza, la routine tacerebbe.
    """
    try:
        out = subprocess.run(
            ["gh", "api", path], capture_output=True, text=True, timeout=30
        )
        if out.returncode == 0:
            return json.loads(out.stdout)
    except (FileNotFoundError, OSError, subprocess.SubprocessError, ValueError):
        pass

    headers = {
        "User-Agent": "frase-del-giorno",
        "Accept": "application/vnd.github+json",
    }
    token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(f"https://api.github.com{path}", headers=headers)
    with urllib.request.urlopen(req, timeout=20) as resp:
        return json.loads(resp.read())


def authenticated_as_akkaz():
    """Senza credenziali /users/akkaz/events risponde 200 con i soli eventi
    pubblici: sembra tutto a posto ma manca meta' della giornata. L'unico modo
    onesto di saperlo e' chiedere a GitHub chi siamo."""
    try:
        return gh("/user").get("login") == "akkaz"
    except Exception:
        return False


def fetch_events():
    """Eventi privati se le credenziali li vedono, altrimenti i soli pubblici."""
    if authenticated_as_akkaz():
        paths = (("/users/akkaz/events?per_page=100", "private"),
                 ("/users/akkaz/events/public?per_page=100", "public"))
    else:
        paths = (("/users/akkaz/events/public?per_page=100", "public"),)

    for path, source in paths:
        try:
            data = gh(path)
            if isinstance(data, list) and data:
                return data, source
        except Exception:
            continue
    return [], "none"


def main():
    events, source = fetch_events()
    now = datetime.now(timezone.utc)
    fresh_cutoff = now - timedelta(hours=WINDOW_HOURS)
    stale_cutoff = now - timedelta(days=STALE_DAYS)

    seen, repos, harvest = set(), set(), []

    for e in events:
        if e.get("type") != "PushEvent":
            continue
        repo = e["repo"]["name"]
        if repo == PROFILE_REPO:          # il repo del profilo non fa notizia
            continue
        if datetime.fromisoformat(e["created_at"].replace("Z", "+00:00")) < stale_cutoff:
            continue

        before, head = e["payload"]["before"], e["payload"]["head"]
        url = f"/repos/{repo}/compare/{before}...{head}"
        if url in seen:
            continue
        if len(seen) >= MAX_COMPARE:
            break                          # gli eventi sono in ordine: cadono i piu' vecchi
        seen.add(url)

        try:
            data = gh(url)
        except Exception:
            continue                       # repo sparito o non accessibile

        for c in data.get("commits", []):
            msg = c["commit"]["message"].splitlines()[0].strip()
            when = datetime.fromisoformat(
                c["commit"]["author"]["date"].replace("Z", "+00:00")
            )
            if when < stale_cutoff:
                continue
            harvest.append((when, repo, msg))

    harvest.sort(key=lambda h: h[0])
    fresh = [h for h in harvest if h[0] >= fresh_cutoff]
    repos = {h[1] for h in fresh}
    times = [h[0].astimezone(ROME) for h in fresh]
    messages = [h[2] for h in fresh if not BORING.match(h[2])]
    night = [t for t in times if t.hour >= 23 or t.hour < 6]

    # nessuna attivita' fresca: si guarda indietro, ma dichiarandolo
    stale_messages, idle_days = [], None
    if not messages:
        stale_messages = [h[2] for h in harvest if not BORING.match(h[2])][-5:]
        if harvest:
            idle_days = (now - harvest[-1][0]).days

    print(
        json.dumps(
            {
                "source": source,
                "empty": not messages,
                "commits": messages[:MAX_COMMITS],
                "commit_count": len(times),
                "repo_count": len(repos),
                "first_commit": times[0].strftime("%H:%M") if times else None,
                "last_commit": times[-1].strftime("%H:%M") if times else None,
                "night_commits": len(night),
                "weekday": datetime.now(ROME).strftime("%A"),
                "stale_commits": stale_messages,
                "idle_days": idle_days,
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
