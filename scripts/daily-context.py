#!/usr/bin/env python3
"""Contesto per la "frase del giorno" del profilo akkaz.

Stampa un JSON con i messaggi di commit delle ultime ore e qualche numero
sulla nottata. Non stampa MAI nomi di repository, organizzazioni o branch:
la genericizzazione parte dai dati, non dalle buone intenzioni di chi legge.

Fonte primaria: i repository clonati accanto a questo nel workspace. La
routine cloud li riceve dalla propria configurazione (che e' privata) e li
legge con git, senza credenziali aggiuntive e senza chiamare l'API.
Se nel workspace c'e' solo il repo del profilo, ripiega sull'API pubblica
di GitHub, che pero' vede solo meta' della giornata: lo dichiara in `source`.

Uso:  python3 scripts/daily-context.py
"""

import json
import os
import re
import subprocess
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

ROME = ZoneInfo("Europe/Rome")
WINDOW_HOURS = 36          # copre "ieri" anche quando la routine gira tardi
STALE_DAYS = 7             # rete di sicurezza per i giorni a vuoto
PROFILE_REPO = "akkaz/akkaz"
MAX_COMMITS = 20
MAX_COMPARE = 25           # tetto alle chiamate API: senza token si viene bloccati a 60/h
MAX_REPOS = 30
AUTHORS = ("akkaz", "giomarco", "baglioni")

# commit che non raccontano niente di interessante
BORING = re.compile(
    r"^(chore|merge\b|revert\b|bump\b|wip\b|fixup\b|initial commit)|frase del giorno",
    re.IGNORECASE,
)


def run(cmd, cwd=None):
    out = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=30)
    if out.returncode != 0:
        raise RuntimeError(out.stderr.strip()[:200])
    return out.stdout


# --------------------------------------------------------------------------
# fonte primaria: i repository clonati nel workspace
# --------------------------------------------------------------------------

def sibling_repos():
    """Repository git accanto a questo, escluso il profilo stesso."""
    try:
        here = Path(run(["git", "rev-parse", "--show-toplevel"],
                        cwd=Path(__file__).resolve().parent).strip())
    except Exception:
        return []

    for parent in (here.parent, here.parent.parent):
        if not parent or not parent.is_dir():
            continue
        found = []
        try:
            for child in parent.iterdir():
                if child == here or not child.is_dir():
                    continue
                git_dir = child / ".git"
                if git_dir.exists():
                    found.append((git_dir.stat().st_mtime, child))
        except (PermissionError, OSError):
            continue
        if found:
            # per mtime, non in ordine alfabetico: se il tetto taglia qualcosa,
            # deve cadere il repo fermo da mesi, non quello di stanotte
            found.sort(reverse=True)
            return [child for _, child in found[:MAX_REPOS]]
    return []


def commits_from_repos(since):
    """(datetime, messaggio, chiave-repo) dei commit di akkaz nei repo trovati.

    La chiave del repo e' un numero, non un nome: serve solo a contare quanti
    progetti sono stati toccati, e non puo' finire per sbaglio in una frase.
    """
    harvest, scanned = [], 0
    fmt = "%aI\x1f%s"
    for index, repo in enumerate(sibling_repos()):
        scanned += 1
        cmd = ["git", "log", "--all", "--no-merges",
               f"--since={since.isoformat()}", f"--pretty=format:{fmt}"]
        for author in AUTHORS:
            cmd.append(f"--author={author}")
        try:
            output = run(cmd, cwd=repo)
        except Exception:
            continue
        for line in output.splitlines():
            if "\x1f" not in line:
                continue
            iso, msg = line.split("\x1f", 1)
            try:
                when = datetime.fromisoformat(iso)
            except ValueError:
                continue
            harvest.append((when, msg.strip(), index))
    return harvest, scanned


# --------------------------------------------------------------------------
# ripiego: API pubblica di GitHub
# --------------------------------------------------------------------------

def gh(path):
    """Chiama l'API GitHub: prima via gh CLI, poi HTTP diretto."""
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


def commits_from_api(since):
    """(datetime, messaggio) ricostruiti dagli eventi push e dall'API compare."""
    if authenticated_as_akkaz():
        paths = (("/users/akkaz/events?per_page=100", "private"),
                 ("/users/akkaz/events/public?per_page=100", "public"))
    else:
        paths = (("/users/akkaz/events/public?per_page=100", "public"),)

    events, source = [], "none"
    for path, src in paths:
        try:
            data = gh(path)
            if isinstance(data, list) and data:
                events, source = data, src
                break
        except Exception:
            continue

    seen, harvest, keys = set(), [], {}
    for e in events:
        if e.get("type") != "PushEvent" or e["repo"]["name"] == PROFILE_REPO:
            continue
        keys.setdefault(e["repo"]["name"], len(keys))
        if datetime.fromisoformat(e["created_at"].replace("Z", "+00:00")) < since:
            continue
        url = f"/repos/{e['repo']['name']}/compare/{e['payload']['before']}...{e['payload']['head']}"
        if url in seen:
            continue
        if len(seen) >= MAX_COMPARE:
            break                          # gli eventi sono in ordine: cadono i piu' vecchi
        seen.add(url)
        try:
            data = gh(url)
        except Exception:
            continue
        for c in data.get("commits", []):
            when = datetime.fromisoformat(
                c["commit"]["author"]["date"].replace("Z", "+00:00")
            )
            if when >= since:
                harvest.append((when,
                                c["commit"]["message"].splitlines()[0].strip(),
                                keys[e["repo"]["name"]]))
    return harvest, source


# --------------------------------------------------------------------------

def main():
    now = datetime.now(timezone.utc)
    fresh_cutoff = now - timedelta(hours=WINDOW_HOURS)
    stale_cutoff = now - timedelta(days=STALE_DAYS)

    harvest, scanned = commits_from_repos(stale_cutoff)
    source = "local"
    if not harvest:                        # nessun repo di lavoro a portata di mano
        harvest, source = commits_from_api(stale_cutoff)

    # stesso commit su piu' branch: il messaggio va raccontato una volta sola
    harvest = list({(w.isoformat(), m): (w, m, k) for w, m, k in harvest}.values())
    harvest.sort(key=lambda h: h[0])

    fresh = [h for h in harvest if h[0] >= fresh_cutoff]
    times = [h[0].astimezone(ROME) for h in fresh]
    messages, seen_msgs = [], set()
    for _, msg, _ in fresh:
        if not BORING.match(msg) and msg not in seen_msgs:
            seen_msgs.add(msg)
            messages.append(msg)
    night = [t for t in times if t.hour >= 23 or t.hour < 6]

    # nessuna attivita' fresca: si guarda indietro, ma dichiarandolo
    stale_messages, idle_days = [], None
    if not messages:
        stale_messages = [m for _, m, _ in harvest if not BORING.match(m)][-5:]
        if harvest:
            idle_days = (now - harvest[-1][0]).days

    print(
        json.dumps(
            {
                "source": source,
                "repos_scanned": scanned,
                "empty": not messages,
                "commits": messages[:MAX_COMMITS],
                "commit_count": len(times),
                "repo_count": len({k for _, _, k in fresh}),
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
