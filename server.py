#!/usr/bin/env python3
"""AI usage dashboard: a local, read-only monitor for the AI tools on this PC.

Reads (never writes) these sources and serves a live dashboard on localhost:
  - Claude Code transcripts   ~/.claude/projects/**/*.jsonl      (tokens per call)
  - Claude plan limits        %APPDATA%/Claude/plan-usage-history.json
                              + plan-readings.json next to this file
  - Ollama server logs        %LOCALAPPDATA%/Ollama/server*.log   (calls only)

Run:  python server.py --open
Standard library only. Nothing leaves this machine.
"""
import argparse
import json
import math
import os
import re
import threading
import time
import webbrowser
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent
HOME = Path.home()
CLAUDE_PROJECTS = HOME / ".claude" / "projects"
PLAN_HISTORY = Path(os.environ.get("APPDATA", str(HOME))) / "Claude" / "plan-usage-history.json"
OLLAMA_LOG_DIR = Path(os.environ.get("LOCALAPPDATA", str(HOME))) / "Ollama"
OLLAMA_MANIFESTS = HOME / ".ollama" / "models" / "manifests"
READINGS = HERE / "plan-readings.json"

# List prices, USD per million tokens: (input, output, cache read).
# Cache writes bill at 1.25x input (5-minute) and 2x input (1-hour).
# Cache-read prices marked "assumed" use the usual 0.1x-input rule.
# Edit this table when prices change.
PRICES = {
    "claude-fable-5-2": (8.00, 40.00, 0.20),
    "claude-mythos-5-2": (8.00, 40.00, 0.20),
    "claude-fable-5-1": (10.00, 50.00, 1.00),   # cache read assumed
    "claude-fable-5": (10.00, 50.00, 1.00),     # cache read assumed
    "claude-opus-5-5": (4.00, 20.00, 0.20),
    "claude-opus-5": (5.00, 25.00, 0.50),       # cache read assumed
    "claude-opus-4-8": (5.00, 25.00, 0.50),     # cache read assumed
    "claude-opus-4-7": (5.00, 25.00, 0.50),     # cache read assumed
    "claude-opus-4-6": (5.00, 25.00, 0.50),     # cache read assumed
    "claude-sonnet-5-5": (2.00, 10.00, 0.10),
    "claude-sonnet-5": (2.00, 10.00, 0.20),     # cache read assumed
    "claude-sonnet-4-6": (3.00, 15.00, 0.30),   # cache read assumed
    "claude-haiku-5-5": (0.10, 0.50, 0.01),     # cache read assumed
    "claude-haiku-4-5": (1.00, 5.00, 0.10),     # cache read assumed
}
FAST_MULTIPLIER = 2.0  # fast mode bills at twice the standard rate
PLAN_PRICE_USD = {"Pro": 20, "Max": 100, "Max 5x": 100, "Max 20x": 200}

FIVE_HOURS_MS = 5 * 3600 * 1000
WEEK_MS = 7 * 86400 * 1000
LIVE_WINDOW_MS = 3 * 60 * 1000
SPARK_BUCKETS = 24


def price_for(model):
    """Longest-prefix match so dated ids (claude-x-20260101) still price."""
    best = None
    for key in PRICES:
        if model.startswith(key) and (best is None or len(key) > len(best)):
            best = key
    return PRICES.get(best) if best else None


def iso_to_ms(ts):
    try:
        return int(datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp() * 1000)
    except (ValueError, AttributeError, TypeError):
        return None


def day_key(ms):
    return datetime.fromtimestamp(ms / 1000).strftime("%Y-%m-%d")


# ---------------------------------------------------------------- Claude Code

_file_cache = {}  # path -> (mtime_ns, size, parsed)


def parse_transcript(path):
    """One transcript -> {session, title, msgs: {message id: record}}."""
    is_subagent = "subagents" in path.parts
    session = path.parent.parent.name if is_subagent else path.stem
    title = None
    last_prompt = None
    msgs = {}
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        for line in fh:
            if '"type"' not in line:
                continue
            try:
                entry = json.loads(line)
            except ValueError:
                continue
            kind = entry.get("type")
            if kind == "custom-title":
                title = entry.get("customTitle") or title
            elif kind == "agent-name":
                title = title or entry.get("agentName")
            elif kind == "last-prompt":
                last_prompt = entry.get("lastPrompt") or last_prompt
            elif kind == "assistant":
                message = entry.get("message") or {}
                usage = message.get("usage")
                model = message.get("model") or ""
                if not usage or not model or model.startswith("<"):
                    continue
                ts = iso_to_ms(entry.get("timestamp"))
                if ts is None:
                    continue
                key = message.get("id") or entry.get("uuid")
                creation = usage.get("cache_creation") or {}
                cw_1h = creation.get("ephemeral_1h_input_tokens") or 0
                cw_total = usage.get("cache_creation_input_tokens") or 0
                cw_5m = creation.get("ephemeral_5m_input_tokens")
                if cw_5m is None:
                    cw_5m = max(cw_total - cw_1h, 0)
                record = {
                    "ts": ts,
                    "model": model,
                    "in": usage.get("input_tokens") or 0,
                    "out": usage.get("output_tokens") or 0,
                    "cw5": cw_5m,
                    "cw1h": cw_1h,
                    "cr": usage.get("cache_read_input_tokens") or 0,
                    "fast": usage.get("speed") == "fast",
                    "side": bool(entry.get("isSidechain")) or is_subagent,
                }
                # A streamed message is logged once per content block; the last
                # line carries the final usage, the first carries the start time.
                if key in msgs:
                    record["ts"] = min(record["ts"], msgs[key]["ts"])
                msgs[key] = record
    return {"session": session, "title": title, "prompt": last_prompt, "msgs": msgs,
            "subagent": is_subagent}


def load_transcripts():
    parsed = []
    if not CLAUDE_PROJECTS.exists():
        return parsed
    seen = set()
    for path in CLAUDE_PROJECTS.rglob("*.jsonl"):
        try:
            stat = path.stat()
        except OSError:
            continue
        seen.add(path)
        cached = _file_cache.get(path)
        if cached and cached[0] == stat.st_mtime_ns and cached[1] == stat.st_size:
            parsed.append(cached[2])
            continue
        try:
            result = parse_transcript(path)
        except OSError:
            continue
        _file_cache[path] = (stat.st_mtime_ns, stat.st_size, result)
        parsed.append(result)
    for stale in set(_file_cache) - seen:
        del _file_cache[stale]
    return parsed


def message_cost(rec):
    price = price_for(rec["model"])
    if not price:
        return 0.0
    p_in, p_out, p_read = price
    cost = (rec["in"] * p_in + rec["out"] * p_out + rec["cw5"] * p_in * 1.25
            + rec["cw1h"] * p_in * 2.0 + rec["cr"] * p_read) / 1_000_000
    return cost * (FAST_MULTIPLIER if rec["fast"] else 1.0)


def collect_claude():
    """Returns (flat message list, sessions dict). Dedupes ids across files."""
    messages = []
    sessions = {}
    seen_ids = set()
    for doc in load_transcripts():
        sess = sessions.setdefault(doc["session"], {"id": doc["session"], "title": None,
                                                    "prompt": None, "msgs": []})
        if not doc["subagent"]:
            sess["title"] = doc["title"] or sess["title"]
            sess["prompt"] = doc["prompt"] or sess["prompt"]
        for key, rec in doc["msgs"].items():
            if key in seen_ids:
                continue
            seen_ids.add(key)
            rec = dict(rec)
            rec["tok"] = rec["in"] + rec["out"] + rec["cw5"] + rec["cw1h"] + rec["cr"]
            rec["cost"] = message_cost(rec)
            rec["session"] = doc["session"]
            messages.append(rec)
            sess["msgs"].append(rec)
    return messages, sessions


def summarize_session(sess, now):
    msgs = sorted(sess["msgs"], key=lambda r: r["ts"])
    if not msgs:
        return None
    start, end = msgs[0]["ts"], msgs[-1]["ts"]
    models = {}
    active_ms = 0
    prev = None
    for rec in msgs:
        slot = models.setdefault(rec["model"], {"tok": 0, "cost": 0.0, "calls": 0})
        slot["tok"] += rec["tok"]
        slot["cost"] += rec["cost"]
        slot["calls"] += 1
        if prev is not None and rec["ts"] - prev < 5 * 60 * 1000:
            active_ms += rec["ts"] - prev
        prev = rec["ts"]
    span = max(end - start, 1)
    spark = [0] * SPARK_BUCKETS
    for rec in msgs:
        idx = min(int((rec["ts"] - start) / span * SPARK_BUCKETS), SPARK_BUCKETS - 1)
        spark[idx] += rec["tok"]
    title = sess["title"] or (sess["prompt"] or "").strip() or "Untitled session"
    return {
        "id": sess["id"],
        "title": title[:90],
        "start": start,
        "end": end,
        "tok": sum(r["tok"] for r in msgs),
        "cost": round(sum(r["cost"] for r in msgs), 4),
        "calls": len(msgs),
        "sideCalls": sum(1 for r in msgs if r["side"]),
        "activeMin": round(active_ms / 60000, 1),
        "models": {m: {"tok": v["tok"], "cost": round(v["cost"], 4), "calls": v["calls"]}
                   for m, v in models.items()},
        "spark": spark,
        "live": now - end < LIVE_WINDOW_MS,
    }


# --------------------------------------------------------------------- Ollama

GIN_RE = re.compile(
    r"\[GIN\]\s+(\d{4}/\d{2}/\d{2}) - (\d{2}:\d{2}:\d{2})\s*\|\s*(\d{3})\s*\|\s*([^|]+?)\s*\|"
    r"[^|]*\|\s*POST\s+\"(/api/chat|/api/generate|/api/embed|/api/embeddings|/v1/[^\"]+)\"")
DUR_RE = re.compile(r"(?:(\d+)h)?(?:(\d+)m(?!s))?(?:([\d.]+)(ms|µs|us|s))?")

_ollama_cache = {}


def parse_duration(text):
    match = DUR_RE.fullmatch(text.strip())
    if not match:
        return 0.0
    hours, minutes, value, unit = match.groups()
    secs = int(hours or 0) * 3600 + int(minutes or 0) * 60
    if value:
        scale = {"s": 1.0, "ms": 1e-3, "µs": 1e-6, "us": 1e-6}[unit]
        secs += float(value) * scale
    return secs


def collect_ollama():
    calls = []
    if OLLAMA_LOG_DIR.exists():
        for path in OLLAMA_LOG_DIR.glob("server*.log"):
            try:
                stat = path.stat()
            except OSError:
                continue
            cached = _ollama_cache.get(path)
            if cached and cached[0] == (stat.st_mtime_ns, stat.st_size):
                calls.extend(cached[1])
                continue
            found = []
            try:
                with open(path, "r", encoding="utf-8", errors="replace") as fh:
                    for line in fh:
                        if "[GIN]" not in line:
                            continue
                        match = GIN_RE.search(line)
                        if not match:
                            continue
                        date, clock, status, dur, route = match.groups()
                        try:
                            when = datetime.strptime(date + " " + clock, "%Y/%m/%d %H:%M:%S")
                        except ValueError:
                            continue
                        found.append({"ts": int(when.timestamp() * 1000),
                                      "ok": status.startswith("2"),
                                      "secs": parse_duration(dur), "route": route})
            except OSError:
                continue
            _ollama_cache[path] = ((stat.st_mtime_ns, stat.st_size), found)
            calls.extend(found)
    installed = []
    if OLLAMA_MANIFESTS.exists():
        for manifest in OLLAMA_MANIFESTS.rglob("*"):
            if manifest.is_file():
                installed.append(manifest.parent.name + ":" + manifest.name)
    present = OLLAMA_LOG_DIR.exists() or (HOME / ".ollama").exists()
    return calls, sorted(set(installed)), present


# ---------------------------------------------------------------- Plan limits

def load_json(path):
    try:
        with open(path, "r", encoding="utf-8-sig") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None


def load_plan_sources():
    readings = []
    doc = load_json(READINGS) or {}
    for item in doc.get("readings", []):
        when = iso_to_ms(item.get("t"))
        if when is None:
            continue
        windows = []
        for win in item.get("windows", []):
            windows.append({"key": win.get("key"), "label": win.get("label"),
                            "pct": win.get("pct"), "resetsAt": iso_to_ms(win.get("resetsAt"))})
        readings.append({"t": when, "plan": item.get("plan"), "windows": windows,
                         "extraUsage": item.get("extraUsage")})
    readings.sort(key=lambda r: r["t"])
    history = []
    for sample in (load_json(PLAN_HISTORY) or {}).get("samples", []):
        used = sample.get("u") or {}
        if "fh" in used or "sd" in used:
            history.append({"t": sample.get("t"), "fh": used.get("fh"), "sd": used.get("sd")})
    history.sort(key=lambda s: s["t"])
    return readings, history


def window_of(reading, key):
    for win in reading["windows"]:
        if win["key"] == key:
            return win
    return None


def local_usage(messages, since_ms):
    tok = sum(r["tok"] for r in messages if r["ts"] > since_ms)
    cost = sum(r["cost"] for r in messages if r["ts"] > since_ms)
    return {"tok": tok, "cost": round(cost, 4)}


def resolve_limits(now, messages):
    """Newest known plan percentages, marked stale once their window has reset."""
    readings, history = load_plan_sources()
    latest = readings[-1] if readings else None
    plan = latest["plan"] if latest else None

    # 5-hour window
    cands = []
    for reading in readings:
        win = window_of(reading, "five_hour")
        if win and win["pct"] is not None:
            cands.append({"t": reading["t"], "pct": win["pct"], "resetsAt": win["resetsAt"],
                          "src": "plan reading"})
    for sample in history:
        if sample["fh"] is not None:
            cands.append({"t": sample["t"], "pct": sample["fh"], "resetsAt": None,
                          "src": "desktop app"})
    cands.sort(key=lambda c: c["t"])
    five = {"key": "five_hour", "label": "5-hour limit", "pct": None, "state": "unknown",
            "resetsAt": None, "readAt": None, "src": None}
    if cands:
        cur = cands[-1]
        if cur["resetsAt"] is None:  # borrow the reset time of the window it falls in
            for other in reversed(cands):
                if other["resetsAt"] and other["t"] <= cur["t"] < other["resetsAt"]:
                    cur["resetsAt"] = other["resetsAt"]
                    break
        if cur["resetsAt"]:
            state = "current" if now < cur["resetsAt"] else "reset"
        else:
            state = "last-known" if now - cur["t"] < FIVE_HOURS_MS else "reset"
        five.update(pct=cur["pct"], state=state, resetsAt=cur["resetsAt"], readAt=cur["t"],
                    src=cur["src"])
        five["since"] = local_usage(messages, cur["t"])
    five["trailing"] = local_usage(messages, now - FIVE_HOURS_MS)

    # weekly window: resets on a fixed 7-day cadence, so one known reset anchors it
    anchor = None
    for reading in reversed(readings):
        win = window_of(reading, "weekly")
        if win and win["resetsAt"]:
            anchor = win["resetsAt"]
            break
    cands = []
    for reading in readings:
        win = window_of(reading, "weekly")
        if win and win["pct"] is not None:
            cands.append({"t": reading["t"], "pct": win["pct"], "src": "plan reading"})
    for sample in history:
        if sample["sd"] is not None:
            cands.append({"t": sample["t"], "pct": sample["sd"], "src": "desktop app"})
    cands.sort(key=lambda c: c["t"])
    weekly = {"key": "weekly", "label": "Weekly · all models", "pct": None, "state": "unknown",
              "resetsAt": None, "readAt": None, "src": None, "windowStart": None}
    next_reset = None
    if anchor:
        steps = math.floor((now - anchor) / WEEK_MS) + 1 if now >= anchor else 0
        next_reset = anchor + steps * WEEK_MS
        weekly["resetsAt"] = next_reset
        weekly["windowStart"] = next_reset - WEEK_MS
    if cands:
        cur = cands[-1]
        if next_reset:
            state = "current" if cur["t"] >= next_reset - WEEK_MS else "reset"
        else:
            state = "last-known"
        weekly.update(pct=cur["pct"], state=state, readAt=cur["t"], src=cur["src"])
        weekly["since"] = local_usage(messages, cur["t"])
    if weekly["windowStart"]:
        weekly["window"] = local_usage(messages, weekly["windowStart"])

    # any other windows the reading listed (per-model weekly caps and so on)
    other = []
    if latest:
        for win in latest["windows"]:
            if win["key"] in ("five_hour", "weekly") or win["pct"] is None:
                continue
            state = "current"
            if win["resetsAt"] and now >= win["resetsAt"]:
                state = "reset"
            other.append({"key": win["key"], "label": win["label"], "pct": win["pct"],
                          "state": state, "resetsAt": win["resetsAt"], "readAt": latest["t"]})
    return {"plan": plan, "planPrice": PLAN_PRICE_USD.get(plan or ""), "fiveHour": five,
            "weekly": weekly, "other": other,
            "extraUsage": latest.get("extraUsage") if latest else None,
            "history": history[-60:]}


# ------------------------------------------------------------------ Assemble

def build_data():
    now = int(time.time() * 1000)
    messages, raw_sessions = collect_claude()
    ollama_calls, ollama_models, ollama_present = collect_ollama()

    days = {}
    models = {}
    for rec in messages:
        day = days.setdefault(day_key(rec["ts"]), {})
        slot = day.setdefault(rec["model"], {"tok": 0, "in": 0, "out": 0, "cw": 0, "cr": 0,
                                             "cost": 0.0, "calls": 0})
        slot["tok"] += rec["tok"]
        slot["in"] += rec["in"]
        slot["out"] += rec["out"]
        slot["cw"] += rec["cw5"] + rec["cw1h"]
        slot["cr"] += rec["cr"]
        slot["cost"] += rec["cost"]
        slot["calls"] += 1
        info = models.setdefault(rec["model"], {"id": rec["model"], "account": "claude",
                                                "lastUsed": 0,
                                                "priced": price_for(rec["model"]) is not None})
        info["lastUsed"] = max(info["lastUsed"], rec["ts"])
    for day in days.values():
        for slot in day.values():
            slot["cost"] = round(slot["cost"], 4)
    for call in ollama_calls:
        slot = days.setdefault(day_key(call["ts"]), {}).setdefault(
            "ollama", {"calls": 0, "fail": 0, "secs": 0.0})
        slot["calls"] += 1
        slot["fail"] += 0 if call["ok"] else 1
        slot["secs"] = round(slot["secs"] + call["secs"], 2)

    sessions = [s for s in (summarize_session(sess, now) for sess in raw_sessions.values()) if s]
    sessions.sort(key=lambda s: s["end"], reverse=True)

    hour_ago = now - 3600 * 1000
    recent = [r for r in messages if r["ts"] > hour_ago]
    by_session = {}
    for rec in recent:
        by_session[rec["session"]] = by_session.get(rec["session"], 0) + rec["tok"]
    top = max(by_session, key=by_session.get) if by_session else None
    top_title = next((s["title"] for s in sessions if s["id"] == top), None)

    accounts = [{
        "id": "claude", "name": "Claude", "kind": "tokens",
        "limits": resolve_limits(now, messages),
        "lastCall": max((r["ts"] for r in messages), default=None),
    }]
    if ollama_present:
        accounts.append({
            "id": "ollama", "name": "Ollama", "kind": "calls", "installed": ollama_models,
            "lastCall": max((c["ts"] for c in ollama_calls), default=None),
        })

    return {
        "now": now,
        "accounts": accounts,
        "models": sorted(models.values(), key=lambda m: m["lastUsed"], reverse=True),
        "days": days,
        "sessions": sessions,
        "burn": {"tok": sum(r["tok"] for r in recent),
                 "cost": round(sum(r["cost"] for r in recent), 4),
                 "calls": len(recent), "topSession": top_title},
        "sources": {
            "transcripts": len(_file_cache),
            "claudeCalls": len(messages),
            "ollamaCalls": len(ollama_calls),
            "planHistory": PLAN_HISTORY.exists(),
            "planReadings": READINGS.exists(),
        },
    }


# --------------------------------------------------------------------- Server

DATA_SOURCE = build_data  # swapped for demo_data by --demo

class Handler(BaseHTTPRequestHandler):
    def _send(self, status, body, content_type):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        route = self.path.split("?", 1)[0]
        if route in ("/", "/index.html"):
            try:
                body = (HERE / "dashboard.html").read_bytes()
            except OSError:
                self._send(500, b"dashboard.html is missing", "text/plain; charset=utf-8")
                return
            self._send(200, body, "text/html; charset=utf-8")
        elif route == "/api/data":
            try:
                body = json.dumps(DATA_SOURCE()).encode("utf-8")
            except Exception as exc:  # surface parse problems in the UI, keep serving
                body = json.dumps({"error": f"{type(exc).__name__}: {exc}"}).encode("utf-8")
                self._send(500, body, "application/json")
                return
            self._send(200, body, "application/json")
        else:
            self._send(404, b"not found", "text/plain; charset=utf-8")

    def log_message(self, fmt, *args):
        pass


def main():
    parser = argparse.ArgumentParser(description="Local AI usage dashboard")
    parser.add_argument("--port", type=int, default=8377)
    parser.add_argument("--open", action="store_true", help="open the dashboard in a browser")
    parser.add_argument("--demo", action="store_true", help="show made-up sample data instead of your logs")
    args = parser.parse_args()
    global DATA_SOURCE
    if args.demo:
        from demo_data import demo_data
        DATA_SOURCE = demo_data
        print("Demo mode: showing made-up sample data, not your logs.", flush=True)
    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    url = f"http://127.0.0.1:{args.port}/"
    print(f"AI usage dashboard running at {url}  (Ctrl+C to stop)", flush=True)
    if args.open:
        threading.Timer(0.6, webbrowser.open, args=(url,)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
