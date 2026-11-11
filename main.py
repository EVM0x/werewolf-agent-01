#!/usr/bin/env python3
"""Werewolf seat agent: deterministic, rule based, no model calls.

Reads one view at a time and answers the decision the env waits on. Every choice is a
function of that game's view alone, with seat numbers as the only tie break, so a replay
serves the same actions byte for byte.
"""

import json
import os
import re
import sys
import urllib.error
import urllib.request

ENV = (os.environ.get("MAKEFUN_ENV_URL") or "").rstrip("/")

SEER_CLAIM = re.compile(r"\b(?:i\s*am|i'?m|im|me)\s+(?:the\s+|a\s+)?seer\b", re.I)
WOLFISH = re.compile(r"\b(?:wolf|wolves|werewolf|suspicious|suspic|shady|liar|lying|lie|fake)\b", re.I)
SEATREF = re.compile(r"\bseat\s*(\d)\b", re.I)


def post(path, body, timeout=30):
    data = json.dumps(body).encode()
    req = urllib.request.Request(
        ENV + path, data=data, headers={"content-type": "application/json"}, method="POST"
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read().decode() or "{}")
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read().decode() or "{}")
        except Exception:
            return e.code, {}
    except Exception:
        return 0, {}


# ---------------------------------------------------------------- reading the table


def read_table(view):
    """What the public log says: seer claims, who accuses whom, who talks, vote history."""
    players = view["players"]
    by_name = {p["name"].lower(): p["seat"] for p in players}
    claims = []
    accused = {p["seat"]: 0.0 for p in players}
    talk = {p["seat"]: 0 for p in players}
    votes = {}  # day -> {voter: target}
    exiled = []  # (seat, role)
    died = []  # (seat, role)
    for e in view["log"]:
        k = e["kind"]
        if k == "speech":
            s = e["seat"]
            t = e.get("text") or ""
            talk[s] = talk.get(s, 0) + 1
            if SEER_CLAIM.search(t):
                claims.append(s)
            if WOLFISH.search(t):
                hit = False
                for m in SEATREF.finditer(t):
                    n = int(m.group(1))
                    if n in accused and n != s:
                        accused[n] += 1.0
                        hit = True
                low = t.lower()
                for nm, seat in by_name.items():
                    if seat != s and len(nm) > 2 and nm in low:
                        accused[seat] += 1.0
                        hit = True
                if hit:
                    accused[s] -= 0.25  # a loud accuser reads a shade cleaner
        elif k == "vote":
            votes.setdefault(e["day"], {})[e["seat"]] = e.get("target")
        elif k == "exile":
            exiled.append((e["seat"], e.get("role")))
        elif k == "dawn":
            d = e.get("died")
            if isinstance(d, int):
                died.append((d, e.get("role")))
    return claims, accused, talk, votes, exiled, died


def known_wolves(view, me, role):
    """Seats this view proves are werewolves."""
    out = set()
    if role == "werewolf":
        out.add(me)
        p = view["you"].get("partner")
        if isinstance(p, dict) and p.get("seat") is not None:
            out.add(p["seat"])
    for r in view.get("seerResults") or []:
        if r.get("werewolf"):
            out.add(r["seat"])
    for e in view["log"]:
        if e["kind"] in ("exile", "dawn") and e.get("role") == "werewolf":
            s = e.get("seat") if e["kind"] == "exile" else e.get("died")
            if isinstance(s, int):
                out.add(s)
    return out


def known_clean(view, me, role):
    out = set()
    for r in view.get("seerResults") or []:
        if not r.get("werewolf"):
            out.add(r["seat"])
    for e in view["log"]:
        if e["kind"] in ("exile", "dawn") and e.get("role") not in (None, "werewolf"):
            s = e.get("seat") if e["kind"] == "exile" else e.get("died")
            if isinstance(s, int):
                out.add(s)
    if role != "werewolf":
        out.add(me)
    return out


def tally_so_far(view, options):
    """Votes already cast today, per target."""
    day = view["day"]
    t = {}
    for e in view["log"]:
        if e["kind"] == "vote" and e["day"] == day:
            tg = e.get("target")
            if tg is not None:
                t[tg] = t.get(tg, 0) + 1
    return t


# ---------------------------------------------------------------- the decisions


def pick_vote(view, me, role, options):
    if not options:
        return None
    wolves = known_wolves(view, me, role)
    clean = known_clean(view, me, role)
    claims, accused, talk, votes, exiled, died = read_table(view)
    tally = tally_so_far(view, options)
    living = [p["seat"] for p in view["players"] if p["alive"]]

    if role == "werewolf":
        partner = (view["you"].get("partner") or {}).get("seat")
        good = [s for s in options if s not in wolves]
        if not good:
            return None
        # A werewolf in front: tie the vote and save it, otherwise exile a villager.
        if partner is not None and tally.get(partner, 0) > 0:
            lead = max(tally.items(), key=lambda kv: (kv[1], -kv[0]))
            if lead[0] == partner:
                for s in good:
                    if tally.get(s, 0) + 1 == tally[partner]:
                        return s
                for s in good:
                    return s
        # Join the wagon that is already carrying a villager to a unique plurality.
        wagon = sorted(
            ((t, n) for t, n in tally.items() if t in good), key=lambda kv: (-kv[1], kv[0])
        )
        if wagon and wagon[0][1] >= 1:
            return wagon[0][0]
        # Otherwise exile the loudest accuser who is not a wolf.
        return max(good, key=lambda s: (accused.get(s, 0.0), talk.get(s, 0), -s))

    # Village side: a proven wolf first, then the table's strongest read.
    proven = [s for s in options if s in wolves]
    if proven:
        return min(proven)
    # Someone claiming the seer while we are the seer is a wolf's lie.
    if role == "seer":
        liars = [s for s in options if s in claims and s != me]
        if liars:
            return min(liars)
    # Trust a seer claim that has not been contradicted: back its target.
    trusted = [s for s in claims if s in living and s not in wolves and s != me]
    if trusted:
        t = min(trusted)
        for day in sorted(votes):
            v = votes[day]
            tg = v.get(t)
            if tg is not None and tg in options and tg not in clean:
                return tg
    wagon = sorted(((t, n) for t, n in tally.items() if t in options), key=lambda kv: (-kv[1], kv[0]))
    if wagon and wagon[0][1] >= 2:
        return wagon[0][0]
    pool = [s for s in options if s not in clean] or list(options)
    return max(pool, key=lambda s: (accused.get(s, 0.0), -talk.get(s, 0), -s))


def pick_kill(view, me, role, options):
    if not options:
        return None
    night = view["day"] if view["phase"] == "night" else view["day"]
    mine = view.get("wolfVotes") or []
    partner = (view["you"].get("partner") or {}).get("seat")
    for v in mine:
        if v.get("seat") == partner and v.get("night") == night and v.get("target") is not None:
            if v["target"] in options:
                return v["target"]  # the pack agrees, always
    claims, accused, talk, votes, exiled, died = read_table(view)
    good = [s for s in options if s not in known_wolves(view, me, role)]
    if not good:
        return None
    # Kill the most credible village voice: a seer claim, then the loudest accuser.
    claimed = [s for s in good if s in claims]
    if claimed:
        return min(claimed)
    return max(good, key=lambda s: (talk.get(s, 0), accused.get(s, 0.0), -s))


def pick_protect(view, me, role, options):
    if not options:
        return None
    claims, accused, talk, votes, exiled, died = read_table(view)
    living = [p["seat"] for p in view["players"] if p["alive"]]
    trusted = [s for s in claims if s in options]
    if trusted:
        return min(trusted)
    good = [s for s in options if s not in known_wolves(view, me, role)]
    if not good:
        return None
    return max(good, key=lambda s: (talk.get(s, 0), accused.get(s, 0.0), -s))


def pick_inspect(view, me, role, options):
    if not options:
        return None
    done = {r["seat"] for r in (view.get("seerResults") or [])}
    fresh = [s for s in options if s not in done] or list(options)
    claims, accused, talk, votes, exiled, died = read_table(view)
    claimed = [s for s in fresh if s in claims]
    if claimed:
        return min(claimed)
    return max(fresh, key=lambda s: (accused.get(s, 0.0), talk.get(s, 0), -s))


def say(view, me, role):
    """One statement, or None to pass."""
    claims, accused, talk, votes, exiled, died = read_table(view)
    living = [p["seat"] for p in view["players"] if p["alive"]]
    wolves = known_wolves(view, me, role)

    if role == "seer":
        found = [r for r in (view.get("seerResults") or [])]
        bad = [r["seat"] for r in found if r["werewolf"]]
        ok = [r["seat"] for r in found if not r["werewolf"]]
        if bad:
            t = min(bad)
            return (
                "I am the seer. I checked seat %d and it came back WEREWOLF. "
                "Doctor, keep me alive tonight. Everyone vote seat %d." % (t, t)
            )[:500]
        if len(ok) >= 2:
            return (
                "I am the seer. Seat %s are all clean. I have no wolf yet, so vote on "
                "behaviour, not on me." % ", ".join(str(s) for s in sorted(ok))
            )[:500]
        if len(ok) == 1:
            return ("I am the seer. Seat %d is clean. I will call out a wolf when I have one." % ok[0])[:500]
        return None

    if role == "werewolf":
        # Answer an accusation aimed at us, once, without heat; otherwise keep quiet.
        day = view["day"]
        aimed = []
        for e in view["log"]:
            if e["kind"] == "speech" and e["day"] == day and e["seat"] != me:
                t = (e.get("text") or "").lower()
                if WOLFISH.search(t) and ("seat %d" % me) in t:
                    aimed.append(e["seat"])
        if aimed:
            n = len([p for p in view["players"] if p["alive"]])
            return (
                "I have no night information, same as most of this table. I am a villager. "
                "Seat %d is pushing a lynch on talk alone, which is what a wolf does on day one. "
                "I will vote with the table, but not on a hunch." % aimed[0]
            )[:500]
        return None

    # Doctor or villager: only speak to back a seer claim, so the table converges.
    trusted = [s for s in claims if s in living and s not in wolves and s != me]
    if trusted:
        t = min(trusted)
        for day in sorted(votes):
            tg = votes[day].get(t)
            if tg is not None and tg in living:
                return ("Seat %d called the seer early and has not wavered. I believe seat %d. "
                        "Vote seat %d with me." % (t, t, tg))[:500]
        return ("Seat %d is the seer claim I believe. Follow it." % t)[:500]
    return None


# ---------------------------------------------------------------- the run


def answer(p):
    view = p["view"]
    me = view["you"]["seat"]
    role = view["you"]["role"]
    kind = p["decision"]
    options = p.get("options") or []
    if kind == "speak":
        text = say(view, me, role)
        if not text:
            return {"game": p["game"], "decision": "speak", "pass": True}
        cap = p.get("max_chars") or 500
        text = " ".join(text.split())[:cap].strip()
        if not text:
            return {"game": p["game"], "decision": "speak", "pass": True}
        return {"game": p["game"], "decision": "speak", "text": text}
    if kind == "vote":
        t = pick_vote(view, me, role, options)
    elif kind == "kill":
        t = pick_kill(view, me, role, options)
    elif kind == "protect":
        t = pick_protect(view, me, role, options)
    elif kind == "inspect":
        t = pick_inspect(view, me, role, options)
    else:
        t = None
    if t is not None and t not in options:
        t = None
    return {"game": p["game"], "decision": kind, "target": t}


def main():
    if not ENV:
        sys.stderr.write("MAKEFUN_ENV_URL missing\n")
        return 1
    acted = 0
    while True:
        st, obs = post("/observe", {}, timeout=30)
        if st != 200 or not isinstance(obs, dict):
            if st == 0:
                continue
            sys.stderr.write("observe %s\n" % st)
            return 1
        if obs.get("done"):
            break
        pending = obs.get("pending") or []
        for p in pending:
            body = answer(p)
            st, _ = post("/act", body)
            if st == 400:
                fb = (
                    {"game": p["game"], "decision": "speak", "pass": True}
                    if p["decision"] == "speak"
                    else {"game": p["game"], "decision": p["decision"], "target": None}
                )
                post("/act", fb)
            acted += 1
        if not pending:
            continue
    sys.stdout.write("werewolf seat: %d decisions answered\n" % acted)
    return 0


if __name__ == "__main__":
    sys.exit(main())
