#!/usr/bin/env python3
import argparse
import concurrent.futures
import json
import os
import sys
import time
import urllib.request
import urllib.error
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
ENDPOINT = "https://opencode.ai/_server"
DEFAULT_WORKSPACE = "wrk_01KEA7B2T0NAVKBJ0T9B2DH63K"
X_SERVER_ID = "bfd684bfc2e4eed05cd0b518f5e4eafd3f3376e3938abb9e536e7c03df831e5c"
X_SERVER_INSTANCE = "server-fn:4"
PER_USD = 1e8
CACHE_DIR = os.path.join(os.path.expanduser("~"), ".cache", "opencode-usage")
CACHE_FILE = os.path.join(CACHE_DIR, "records.jsonl")
CACHE_AGE = 300


def load_auth(cli_auth):
    if cli_auth:
        return cli_auth
    env = os.environ.get("OC_USAGE_AUTH")
    if env:
        return env
    for path in (os.path.join(HERE, "auth.txt"), os.path.join(HERE, ".auth")):
        if os.path.exists(path):
            with open(path) as fh:
                return fh.read().strip()
    return None


class ParseError(Exception):
    pass


def _ws(s, i):
    while i < len(s) and s[i] in " \t\r\n":
        i += 1
    return i


def _string(s, i):
    i += 1
    out = []
    while True:
        c = s[i]
        if c == "\\":
            nxt = s[i + 1]
            out.append(
                {"n": "\n", "t": "\t", "r": "\r", "\\": "\\", '"': '"'}.get(nxt, nxt)
            )
            i += 2
        elif c == '"':
            return "".join(out), i + 1
        else:
            out.append(c)
            i += 1


def _number(s, i):
    j = i
    if s[j] == "-":
        j += 1
    while j < len(s) and s[j].isdigit():
        j += 1
    return int(s[i:j]), j


def _ref(s, i, reg):
    j = s.index("[", i) + 1
    k = s.index("]", j)
    n = int(s[j:k])
    m = _ws(s, k + 1)
    if m < len(s) and s[m] == "=":
        v, m2 = _parse_value(s, m + 1, reg)
        while len(reg) <= n:
            reg.append(None)
        reg[n] = v
        return v, m2
    if n < len(reg):
        return reg[n], m
    raise ParseError(f"undefined ref $R[{n}]")


def _array(s, i, reg):
    i = _ws(s, i + 1)
    out = []
    if i < len(s) and s[i] == "]":
        return out, i + 1
    while True:
        v, i = _parse_value(s, i, reg)
        out.append(v)
        i = _ws(s, i)
        if i < len(s) and s[i] == ",":
            i += 1
            continue
        if i < len(s) and s[i] == "]":
            return out, i + 1
        raise ParseError(f"expected , or ] at {i}")


def _object(s, i, reg):
    i = _ws(s, i + 1)
    out = {}
    if i < len(s) and s[i] == "}":
        return out, i + 1
    while True:
        i = _ws(s, i)
        if i < len(s) and s[i] == '"':
            key, i = _string(s, i)
        else:
            j = i
            while j < len(s) and s[j] not in ":{}, ":
                j += 1
            key = s[i:j]
            i = j
        i = _ws(s, i)
        if s[i] != ":":
            raise ParseError(f"expected : at {i}")
        v, i = _parse_value(s, i + 1, reg)
        out[key] = v
        i = _ws(s, i)
        if i < len(s) and s[i] == ",":
            i += 1
            continue
        if i < len(s) and s[i] == "}":
            return out, i + 1
        raise ParseError(f"expected , or }} at {i}")


def _parse_value(s, i, reg):
    i = _ws(s, i)
    c = s[i]
    if c == "[":
        return _array(s, i, reg)
    if c == "{":
        return _object(s, i, reg)
    if c == '"':
        return _string(s, i)
    if c == "(":
        v, i = _parse_value(s, i + 1, reg)
        return v, _ws(s, i) + 1
    if c == "$":
        return _ref(s, i, reg)
    if s.startswith("new ", i) or s.startswith("new(", i):
        j = s.index("(", i)
        return _parse_value(s, j, reg)
    if c == "n":
        return None, i + 4
    if c == "t":
        return True, i + 4
    if c == "f":
        return False, i + 5
    if c == "-" or c.isdigit():
        return _number(s, i)
    raise ParseError(f"unexpected char at {i}: {s[i : i + 20]!r}")


def decode(body):
    payload = body.split(";", 2)[2]
    marker = "$R[0]="
    i = payload.index(marker) + len(marker)
    reg = [None]
    v, _ = _parse_value(payload, i, reg)
    reg[0] = v
    return reg[0]


def request_page(page, workspace, auth):
    payload = {
        "t": {
            "t": 9,
            "i": 0,
            "l": 2,
            "a": [{"t": 1, "s": workspace}, {"t": 0, "s": page}],
            "o": 0,
        },
        "f": 31,
        "m": [],
    }
    headers = {
        "accept": "*/*",
        "content-type": "application/json",
        "origin": "https://opencode.ai",
        "referer": f"https://opencode.ai/workspace/{workspace}/usage",
        "x-server-id": X_SERVER_ID,
        "x-server-instance": X_SERVER_INSTANCE,
        "user-agent": "Mozilla/5.0",
        "cookie": auth,
    }
    req = urllib.request.Request(
        ENDPOINT,
        data=json.dumps(payload).encode("utf-8"),
        headers=headers,
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            body = resp.read().decode("utf-8")
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"HTTP {e.code}: {e.read().decode(errors='replace')[:300]}")
    if body.lstrip().startswith("{"):
        raise RuntimeError(body[:300])
    return decode(body)


def parse_dt(s):
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def fetch_all(workspace, auth, max_pages, since_dt, workers=16):
    seen = {}
    start = 0
    while start <= max_pages:
        end = min(start + workers, max_pages + 1)
        pages = list(range(start, end))
        results = {}
        pending = pages[:]
        for attempt in range(3):
            if not pending:
                break
            with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as ex:
                futures = {ex.submit(request_page, p, workspace, auth): p for p in pending}
                for fut in concurrent.futures.as_completed(futures):
                    p = futures[fut]
                    try:
                        results[p] = fut.result()
                    except Exception:
                        results[p] = None
            pending = [p for p in pages if results.get(p) is None]
        done = False
        for p in pages:
            recs = results.get(p)
            if not recs:
                continue
            for r in recs:
                seen[r["id"]] = r
            if len(recs) < 50:
                done = True
            if since_dt is not None and parse_dt(recs[-1]["timeCreated"]) < since_dt:
                done = True
        if start and start % 200 == 0:
            print(f"  ...page {start}, {len(seen):,} requests", file=sys.stderr)
        if done or end > max_pages:
            break
        start = end
    return list(seen.values())


def load_cached(force=False):
    if not os.path.exists(CACHE_FILE):
        return None
    mtime = os.path.getmtime(CACHE_FILE)
    if not force and time.time() - mtime > CACHE_AGE:
        return None
    rows = []
    with open(CACHE_FILE) as fh:
        for line in fh:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    return rows


def save_cache(rows):
    os.makedirs(CACHE_DIR, exist_ok=True)
    with open(CACHE_FILE, "w") as fh:
        for r in rows:
            fh.write(json.dumps(r) + "\n")


def normalize(r):
    cache_write = (r.get("cacheWrite5mTokens") or 0) + (r.get("cacheWrite1hTokens") or 0)
    return {
        "id": r["id"],
        "time": parse_dt(r["timeCreated"]),
        "model": r["model"],
        "provider": r["provider"],
        "session": (r.get("sessionID") or "").split("_")[-1],
        "key": r.get("keyID") or "",
        "input": (r.get("inputTokens") or 0) + cache_write,
        "cache_write": cache_write,
        "cache_read": r.get("cacheReadTokens") or 0,
        "output": r.get("outputTokens") or 0,
        "reasoning": r.get("reasoningTokens") or 0,
        "cost": (r.get("cost") or 0) / PER_USD,
    }


def money(x):
    if x >= 100:
        return f"${x:,.2f}"
    if x >= 1:
        return f"${x:,.4f}"
    return f"${x:.4f}"


def fmt_num(n):
    return f"{n:,}"


def fmt_time(dt):
    local = dt.astimezone()
    return local.strftime("%b %d, %I:%M %p").replace(" 0", " ")


def print_table(header, rows, json_out):
    if json_out:
        print(json.dumps([dict(zip(header, r)) for r in rows], indent=2))
        return
    widths = [len(h) for h in header]
    for r in rows:
        for i, cell in enumerate(r):
            widths[i] = max(widths[i], len(str(cell)))
    line = "  ".join(h.ljust(widths[i]) for i, h in enumerate(header))
    print(line)
    print("  ".join("-" * widths[i] for i in range(len(header))))
    for r in rows:
        print("  ".join(str(c).ljust(widths[i]) for i, c in enumerate(r)))


def main():
    ap = argparse.ArgumentParser(description="OpenCode.ai usage stats")
    ap.add_argument(
        "command",
        nargs="?",
        default="summary",
        choices=[
            "summary",
            "requests",
            "sessions",
            "models",
            "providers",
            "days",
            "hours",
            "keys",
            "raw",
        ],
    )
    ap.add_argument(
        "--since",
        type=int,
        default=None,
        metavar="DAYS",
        help="only consider the last N days",
    )
    ap.add_argument("--model", default=None, help="filter by model name")
    ap.add_argument("--provider", default=None, help="filter by provider")
    ap.add_argument("--top", type=int, default=20, help="limit for list views")
    ap.add_argument("--max-pages", type=int, default=100000, help="max pages to fetch")
    ap.add_argument("--workers", type=int, default=16, help="concurrent page requests")
    ap.add_argument(
        "--workspace", default=os.environ.get("OC_WORKSPACE", DEFAULT_WORKSPACE)
    )
    ap.add_argument(
        "--auth", default=None, help="full cookie string, overrides auth.txt"
    )
    ap.add_argument(
        "--cached", action="store_true", help="use cached data, don't fetch"
    )
    ap.add_argument("--refresh", action="store_true", help="force refetch from the API")
    ap.add_argument("--json", action="store_true", help="output JSON")
    args = ap.parse_args()

    auth = load_auth(args.auth)
    if not auth:
        print(
            "error: no auth cookie. set OC_USAGE_AUTH, --auth, or create auth.txt",
            file=sys.stderr,
        )
        sys.exit(1)

    since_dt = None
    if args.since:
        since_dt = datetime.now(timezone.utc) - timedelta(days=args.since)

    rows = None
    if args.cached:
        rows = load_cached(force=True)
        if rows is None:
            print("error: no cache available, run without --cached", file=sys.stderr)
            sys.exit(1)
    elif not args.refresh:
        rows = load_cached()
    if rows is None:
        print("fetching usage history...", file=sys.stderr)
        try:
            rows = fetch_all(args.workspace, auth, args.max_pages, since_dt,
                             workers=args.workers)
            save_cache(rows)
        except RuntimeError as e:
            print(f"error: {e}", file=sys.stderr)
            sys.exit(1)
        print(f"fetched {len(rows):,} requests", file=sys.stderr)

    recs = [normalize(r) for r in rows]
    if since_dt is not None:
        recs = [r for r in recs if r["time"] >= since_dt]
    if args.model:
        recs = [r for r in recs if r["model"] == args.model]
    if args.provider:
        recs = [r for r in recs if r["provider"] == args.provider]
    recs.sort(key=lambda r: r["time"], reverse=True)

    if not recs:
        print("no records match")
        return

    if args.command == "raw":
        for r in recs:
            out = dict(r)
            out["time"] = out["time"].isoformat()
            print(json.dumps(out))
        return

    if args.command == "requests":
        header = ["Date", "Model", "Type", "Input", "Cache", "Output", "Cost", "Session"]
        table = [
            [
                fmt_time(r["time"]),
                r["model"],
                "billed" if r["cost"] > 0 else "free",
                fmt_num(r["input"]),
                fmt_num(r["cache_read"]),
                fmt_num(r["output"]),
                money(r["cost"]),
                r["session"],
            ]
            for r in recs[: args.top]
        ]
        print_table(header, table, args.json)
        return

    def build_row(key, g):
        n = len(g)
        inp = sum(x["input"] for x in g)
        cr = sum(x["cache_read"] for x in g)
        billed = sum(1 for x in g if x["cost"] > 0)
        return {
            "key": key,
            "requests": n,
            "billed": billed,
            "free": n - billed,
            "input": inp,
            "cache": cr,
            "output": sum(x["output"] for x in g),
            "reasoning": sum(x["reasoning"] for x in g),
            "cost": sum(x["cost"] for x in g),
            "hit": (cr / (inp + cr) * 100) if inp + cr else 0.0,
        }

    def aggregate(key):
        groups = defaultdict(list)
        for r in recs:
            groups[r[key]].append(r)
        out = [build_row(k, g) for k, g in groups.items()]
        out.sort(key=lambda x: x["cost"], reverse=True)
        return out

    def render(first_col, rows, json_out):
        header = [first_col, "Requests", "Billed", "Free", "Input", "Cache",
                  "Output", "Hit%", "Cost"]
        table = [
            [
                r["key"],
                fmt_num(r["requests"]),
                fmt_num(r["billed"]),
                fmt_num(r["free"]),
                fmt_num(r["input"]),
                fmt_num(r["cache"]),
                fmt_num(r["output"]),
                f"{r['hit']:.1f}%",
                money(r["cost"]),
            ]
            for r in rows
        ]
        print_table(header, table, json_out)

    totals = build_row("overall", recs)
    by_model = aggregate("model")
    by_day = defaultdict(list)
    for r in recs:
        by_day[r["time"].astimezone().date()].append(r)
    days = [
        build_row(str(d), g)
        for d, g in sorted(by_day.items(), key=lambda kv: kv[0], reverse=True)
    ]

    if args.command == "summary":
        print("==", "OVERALL", "==")
        print(f"  requests : {fmt_num(totals['requests'])}  (billed {fmt_num(totals['billed'])} / free {fmt_num(totals['free'])})")
        print(f"  cost     : {money(totals['cost'])}")
        print(f"  input    : {fmt_num(totals['input'])} tokens (not from cache)")
        print(f"  cache    : {fmt_num(totals['cache'])} tokens read  (hit rate {totals['hit']:.1f}%)")
        print(f"  output   : {fmt_num(totals['output'])} tokens")
        print(f"  reasoning: {fmt_num(totals['reasoning'])} tokens")
        print()
        print("==", "PER DAY", "==")
        render("Date", days[: args.top], args.json)
        print()
        print("==", "PER MODEL", "==")
        render("Model", by_model, args.json)
        return

    if args.command == "sessions":
        render("Session", aggregate("session"), args.json)
        return

    if args.command == "models":
        render("Model", by_model, args.json)
        return

    if args.command == "providers":
        render("Provider", aggregate("provider"), args.json)
        return

    if args.command == "days":
        render("Date", days, args.json)
        return

    if args.command == "hours":
        by_hour = defaultdict(list)
        for r in recs:
            by_hour[r["time"].astimezone().strftime("%Y-%m-%d %H:00")].append(r)
        hours = [build_row(h, g) for h, g in sorted(by_hour.items(), reverse=True)]
        render("Hour", hours, args.json)
        return

    if args.command == "keys":
        render("Key", aggregate("key"), args.json)
        return


if __name__ == "__main__":
    main()
