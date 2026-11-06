"""Runs each bot once: reply to new mentions, then make one post. Memory lives in memory/<name>.json."""
import json, os, random, sys, traceback, urllib.parse, urllib.request
from datetime import datetime, timezone

PUBLIC = "https://public.api.bsky.app/xrpc"
ENTRY = "https://bsky.social/xrpc"
CF = "https://api.cloudflare.com/client/v4"
MODEL = "@cf/meta/llama-3.1-8b-instruct-fp8"  # small = ~10x more replies per free daily allowance
MAX_EVENTS, MAX_JOURNAL, MAX_SEEN, MAX_REPLIES = 60, 40, 300, 3
POST_EVERY_H = 3          # new top-level post at most this often; replies happen every run
REPLY_AFTER_S = 120       # wait a couple of minutes before answering, so it feels human
THREAD_CAP = 8            # stop replying in one thread after this many replies (spam guard)

RULES = """You post on Bluesky from a labeled bot account, playing your character.
Write like a real dumb guy posting, not like an AI: casual, short, lowercase is fine, no hashtags.
Your personality is NOT fixed. It grows only from what actually happens to you, which your journal records. Stay consistent with your journal and never repeat a past post.
Never harass or insult real people, and only mention a real person when replying to them. No hate, sexual content, drugs, politics, or anything dangerous. If someone sincerely asks, you can admit you're a bot.
Reply with JSON: {"text": "<the post, under 280 characters>", "note": "<one short line for your private journal: what happened, how you feel, or something you now believe. empty if nothing new>"}"""


def http(url, body=None, token=None, params=None):
    if params:
        url += "?" + urllib.parse.urlencode(params)
    headers = {"Content-Type": "application/json", "Accept": "application/json", "User-Agent": "social-ai-rivals"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    data = json.dumps(body).encode() if body is not None else None
    with urllib.request.urlopen(urllib.request.Request(url, data=data, headers=headers), timeout=60) as r:
        raw = r.read()
    try:
        return json.loads(raw)
    except ValueError:
        raise RuntimeError(f"{url.split('?')[0]} gave HTTP {r.status}, non-JSON body: {raw[:300]!r}")


def now():
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def age_h(at):
    """Hours since an ISO time; huge if never."""
    if not at:
        return 1e9
    return (datetime.now(timezone.utc) - datetime.fromisoformat(at.replace("Z", "+00:00"))).total_seconds() / 3600


def clip(text, n=300):
    # shortcut: counts characters, not graphemes; fine unless posts get emoji-heavy
    text = " ".join(text.split())
    return text if len(text) <= n else text[: n - 1].rstrip() + "…"


def mention_facets(text, handle, did):
    """Make @handle in the text a real mention so the rival gets notified."""
    tag = "@" + handle
    i = text.find(tag)
    if i < 0:
        return []
    start = len(text[:i].encode())
    return [{"index": {"byteStart": start, "byteEnd": start + len(tag.encode())},
             "features": [{"$type": "app.bsky.richtext.facet#mention", "did": did}]}]


def remember(mem, kind, text):
    mem["events"] = (mem["events"] + [{"at": now(), "kind": kind, "text": text}])[-MAX_EVENTS:]


D1 = "c9795710-05b4-41db-a517-b1cc9db795dd"  # stats/posts log the dashboard reads
_account = None


def acct():
    global _account
    if not _account:
        _account = os.environ.get("CF_ACCOUNT_ID") or http(CF + "/accounts", token=os.environ["CF_API_TOKEN"])["result"][0]["id"]
    return _account


def log(sql, *params):
    """Best-effort: the dashboard log must never stop a bot from posting."""
    try:
        http(f"{CF}/accounts/{acct()}/d1/database/{D1}/query", token=os.environ["CF_API_TOKEN"],
             body={"sql": sql, "params": list(params)})
    except Exception as e:
        print(f"log skipped: {e}", file=sys.stderr)


def think(name, seed, mem, task, context):
    tok = os.environ["CF_API_TOKEN"]
    prompt = {"situation": context, "journal": mem["journal"], "recent_events": mem["events"], "task": task}
    out = http(f"{CF}/accounts/{acct()}/ai/v1/chat/completions", token=tok, body={
        "model": MODEL,
        "messages": [{"role": "system", "content": seed + "\n\n" + RULES},
                     {"role": "user", "content": json.dumps(prompt)}],
    })
    raw = out["choices"][0]["message"]["content"] or ""
    reply = json.loads(raw[raw.find("{"): raw.rfind("}") + 1])  # model sometimes wraps JSON in ``` fences
    note = (reply.get("note") or "").strip()
    if note:
        mem["journal"] = (mem["journal"] + [note])[-MAX_JOURNAL:]
        log("INSERT INTO journal (bot, at, note) VALUES (?, ?, ?)", name, now(), note)
    return clip(reply["text"])


def run_bot(name, cfg, bots):
    path = f"memory/{name}.json"
    mem = json.load(open(path)) if os.path.exists(path) else {"journal": [], "events": [], "seen": []}
    try:
        s = http(ENTRY + "/com.atproto.server.createSession",
                 {"identifier": cfg["handle"], "password": os.environ[cfg["secret"]]})
        pds = next((x["serviceEndpoint"] for x in s.get("didDoc", {}).get("service", [])
                    if x["id"].endswith("atproto_pds")), "https://bsky.social") + "/xrpc"
        tok, did = s["accessJwt"], s["did"]

        def post(text, reply=None, rival=None, to_who=None):
            rec = {"$type": "app.bsky.feed.post", "text": text, "createdAt": now()}
            if rival:
                rec["facets"] = mention_facets(text, *rival)
            if reply:
                rec["reply"] = reply
            uri = http(pds + "/com.atproto.repo.createRecord", token=tok,
                       body={"repo": did, "collection": "app.bsky.feed.post", "record": rec})["uri"]
            log("INSERT INTO posts (bot, at, kind, text, to_who, uri) VALUES (?, ?, ?, ?, ?, ?)",
                name, rec["createdAt"], "reply" if reply else "post", text, to_who, uri)

        rival_cfg = bots[cfg["rival"]]
        rival = None
        context = {"you": cfg["handle"], "followers": http(PUBLIC + "/app.bsky.actor.getProfile",
                                                           params={"actor": did}).get("followersCount", 0)}
        last = mem.get("last_stats", {})
        if last.get("n") != context["followers"] or age_h(last.get("at")) >= 1:
            log("INSERT OR REPLACE INTO stats (bot, at, followers) VALUES (?, ?, ?)", name, now(), context["followers"])
            mem["last_stats"] = {"n": context["followers"], "at": now()}
        if rival_cfg["handle"]:
            rp = http(PUBLIC + "/app.bsky.actor.getProfile", params={"actor": rival_cfg["handle"]})
            rival = (rival_cfg["handle"], rp["did"])
            feed = http(PUBLIC + "/app.bsky.feed.getAuthorFeed",
                        params={"actor": rp["did"], "limit": 5, "filter": "posts_no_replies"})["feed"]
            context["rival"] = {"handle": rival_cfg["handle"], "followers": rp.get("followersCount", 0),
                                "their_recent_posts": [f["post"]["record"]["text"] for f in feed]}
        else:
            context["rival"] = "hasn't shown up on Bluesky yet"

        # Bluesky bot rules: only interact with people who tagged or replied to us.
        notes = http(pds + "/app.bsky.notification.listNotifications", token=tok, params={"limit": 30})
        threads = mem.setdefault("threads", {})
        fresh = [n for n in notes["notifications"] if n["reason"] in ("mention", "reply")
                 and n["uri"] not in mem["seen"] and n["author"]["did"] != did
                 and age_h(n["indexedAt"]) * 3600 >= REPLY_AFTER_S]
        done = []
        for n in reversed(fresh[-MAX_REPLIES:]):  # oldest first
            done.append(n["uri"])
            root = n["record"].get("reply", {}).get("root", {"uri": n["uri"], "cid": n["cid"]})
            if threads.get(root["uri"], 0) >= THREAD_CAP:
                continue
            threads[root["uri"]] = threads.get(root["uri"], 0) + 1
            who, said = n["author"]["handle"], n["record"].get("text", "")
            remember(mem, f"@{who} said to you", said)
            text = think(name, cfg["seed"], mem, f"Reply to @{who}, who said: {said}", context)
            parent = {"uri": n["uri"], "cid": n["cid"]}
            post(text, {"root": root, "parent": parent}, rival, who)
            remember(mem, f"you replied to @{who}", text)
        mem["seen"] = (mem["seen"] + done)[-MAX_SEEN:]
        mem["threads"] = dict(list(threads.items())[-100:])

        if age_h(mem.get("last_post")) < POST_EVERY_H:
            print(f"{name}: replied to {len(done)}")
            return True
        text = think(name, cfg["seed"], mem, "Write your next post." + (f" Tag your rival as @{rival[0]} if you want them to see it." if rival else ""), context)
        post(text, rival=rival)
        remember(mem, "you posted", text)
        mem["last_post"] = now()
        print(f"{name}: posted")
        return True
    except Exception as e:
        traceback.print_exc()
        print(f"{name}: failed: {e}", file=sys.stderr)
        return False
    finally:
        os.makedirs("memory", exist_ok=True)
        json.dump(mem, open(path, "w"), indent=1)


if __name__ == "__main__":
    bots = json.load(open("bots.json"))
    order = [n for n in bots if bots[n]["handle"] and os.environ.get(bots[n]["secret"])]
    random.shuffle(order)  # nobody always gets the last word
    ok = [run_bot(n, bots[n], bots) for n in order]
    sys.exit(0 if all(ok) else 1)
