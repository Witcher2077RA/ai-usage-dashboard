"""Made-up sample data for `python server.py --demo`.

Lets anyone try the dashboard without any AI logs on their machine, and is used for the
README screenshot. Same shape as build_data() in server.py. Numbers are invented but
plausible, and are generated relative to "now" so the page always looks current.
"""
import random
import time
from datetime import datetime, timedelta

HOUR = 3600 * 1000
DAY = 24 * HOUR
WEEK = 7 * DAY

# (model id, input $/M, output $/M, cache read $/M) - illustrative list prices
MODELS = [("claude-opus-5-5", 4.0, 20.0, 0.20), ("claude-sonnet-5-5", 2.0, 10.0, 0.10),
          ("claude-haiku-5-5", 0.10, 0.50, 0.01)]

SESSIONS = [  # title, model, hours ago it started, length in minutes, size
    ("Refactor the auth middleware", "claude-opus-5-5", 0.6, 34, 1.4),
    ("Write unit tests for the parser", "claude-sonnet-5-5", 2.5, 48, 1.0),
    ("Summarise meeting notes", "claude-haiku-5-5", 5.0, 9, 0.2),
    ("Debug the CSV importer", "claude-opus-5-5", 22, 61, 1.8),
    ("Draft the project README", "claude-sonnet-5-5", 27, 25, 0.6),
    ("Explore the codebase", "claude-sonnet-5-5", 50, 40, 0.9),
    ("Plan the database migration", "claude-opus-5-5", 75, 55, 1.5),
]


def _usage(rng, model, scale):
    _, p_in, p_out, p_cr = next(m for m in MODELS if m[0] == model)
    cr = int(rng.uniform(0.6, 1.4) * 2_400_000 * scale)
    cw = int(cr * rng.uniform(0.03, 0.06))
    out = int(cr * rng.uniform(0.006, 0.012))
    inp = int(rng.uniform(40, 200) * scale)
    cost = (inp * p_in + out * p_out + cr * p_cr + cw * p_in * 1.25) / 1e6
    calls = max(1, int(cr / 95_000))
    return {"tok": inp + out + cr + cw, "in": inp, "out": out, "cw": cw, "cr": cr,
            "cost": round(cost, 4), "calls": calls}


def demo_data():
    rng = random.Random(7)
    now = int(time.time() * 1000)
    today = datetime.now().date()

    days = {}
    for back in range(14):
        key = (today - timedelta(days=back)).isoformat()
        if back not in (5, 6, 12) or back == 0:            # a few quiet days
            day = {}
            for model, *_ in MODELS:
                if rng.random() < 0.75:
                    day[model] = _usage(rng, model, rng.uniform(0.3, 1.6))
            if rng.random() < 0.6:
                day["ollama"] = {"calls": rng.randint(3, 30), "fail": rng.choice((0, 0, 1)),
                                 "secs": round(rng.uniform(20, 300), 2)}
            if day:
                days[key] = day

    sessions = []
    for i, (title, model, ago, minutes, size) in enumerate(SESSIONS):
        start = now - int(ago * HOUR)
        end = min(now - 40_000 if i == 0 else now, start + minutes * 60_000)
        use = _usage(rng, model, size)
        spark = [0] * 24
        for _ in range(use["calls"]):
            spark[rng.randint(0, 23)] += use["tok"] // use["calls"]
        sessions.append({
            "id": "demo-%02d" % i, "title": title, "start": start, "end": end,
            "tok": use["tok"], "cost": use["cost"], "calls": use["calls"], "sideCalls": rng.randint(0, 3),
            "activeMin": round(minutes * rng.uniform(0.6, 0.9), 1),
            "models": {model: {"tok": use["tok"], "cost": use["cost"], "calls": use["calls"]}},
            "spark": spark, "live": i == 0,
        })

    recent = sessions[0]
    week_start = now - int(3.2 * DAY)
    limits = {
        "plan": "Pro", "planPrice": 20,
        "fiveHour": {"key": "five_hour", "label": "5-hour limit", "pct": 38, "state": "current",
                     "resetsAt": now + int(2.4 * HOUR), "readAt": now - 20 * 60_000, "src": "plan reading",
                     "since": {"tok": 1_850_000, "cost": 1.12},
                     "trailing": {"tok": recent["tok"] + sessions[1]["tok"],
                                  "cost": round(recent["cost"] + sessions[1]["cost"], 4)}},
        "weekly": {"key": "weekly", "label": "Weekly · all models", "pct": 41, "state": "current",
                   "resetsAt": week_start + WEEK, "readAt": now - 20 * 60_000, "src": "plan reading",
                   "windowStart": week_start, "since": {"tok": 1_850_000, "cost": 1.12},
                   "window": {"tok": 48_200_000, "cost": 31.4}},
        "other": [{"key": "weekly_opus", "label": "Weekly · Opus", "pct": 27, "state": "current",
                   "resetsAt": week_start + WEEK, "readAt": now - 20 * 60_000}],
        "extraUsage": False,
        "history": [{"t": now - (40 - k) * 2 * HOUR, "fh": rng.randint(5, 70), "sd": 10 + k} for k in range(31)],
    }
    return {
        "now": now,
        "accounts": [
            {"id": "claude", "name": "Claude", "kind": "tokens", "limits": limits, "lastCall": recent["end"]},
            {"id": "ollama", "name": "Ollama", "kind": "calls", "installed": ["llama3.2:3b", "gemma3:4b"],
             "lastCall": now - 3 * HOUR},
        ],
        "models": [{"id": m, "account": "claude", "lastUsed": now - k * HOUR, "priced": True}
                   for k, (m, *_) in enumerate(MODELS)],
        "days": days,
        "sessions": sessions,
        "burn": {"tok": recent["tok"], "cost": recent["cost"], "calls": recent["calls"],
                 "topSession": recent["title"]},
        "sources": {"transcripts": len(sessions), "claudeCalls": sum(s["calls"] for s in sessions),
                    "ollamaCalls": sum(d.get("ollama", {}).get("calls", 0) for d in days.values()),
                    "planHistory": True, "planReadings": True, "demo": True},
    }
