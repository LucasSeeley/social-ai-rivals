"""Runs each bot once: reply to new mentions, then make one post. Memory lives in memory/<name>.json."""
import json, os, sys, urllib.parse, urllib.request
from datetime import datetime, timezone

PUBLIC = "https://public.api.bsky.app/xrpc"
ENTRY = "https://bsky.social/xrpc"
MODEL_URL = "https://models.github.ai/inference/chat/completions"
MODEL = "openai/gpt-4o-mini"
MAX_EVENTS, MAX_JOURNAL, MAX_SEEN, MAX_REPLIES = 60, 40, 300, 3

RULES = """You post on Bluesky from a labeled bot account, playing your character.
Write like a real dumb guy posting, not like an AI: casual, short, lowercase is fine, no hashtags.
Your personality is NOT fixed. It grows only from what actually happens to you, which your journal records. Stay consistent with your journal and never repeat a past post.
Never harass or insult real people, and only mention a real person when replying to them. No hate, sexual content, drugs, politics, or anything dangerous. If someone sincerely asks, you can admit you're a bot.
Reply with JSON: {"text": "<the post, under 280 characters>", "note": "<one short line for your private journal: what happened, how you feel, or something you now believe. empty if nothing new>"}"""


def http(url, body=None, token=None, params=None):
    if params:
        url += "?" + urllib.parse.urlencode(params)
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    data = json.dumps(body).encode() if body is not None else None
    with urllib.request.urlopen(urllib.request.Request(url, data=data, headers=headers), timeout=60) as r:
        return json.load(r)


def now():
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


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


def think(seed, mem, task, context):
    prompt = {"situation": context, "journal": mem["journal"], "recent_events": mem["events"], "task": task}
    out = http(MODEL_URL, token=os.environ["GITHUB_TOKEN"], body={
        "model": MODEL,
        "response_format": {"type": "json_object"},
        "messages": [{"role": "system", "content": seed + "\n\n" + RULES},
                     {"role": "user", "content": json.dumps(prompt)}],
    })
    reply = json.loads(out["choices"][0]["message"]["content"])
    note = (reply.get("note") or "").strip()
    if note:
        mem["journal"] = (mem["journal"] + [note])[-MAX_JOURNAL:]
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

        def post(text, reply=None, rival=None):
            rec = {"$type": "app.bsky.feed.post", "text": text, "createdAt": now()}
            if rival:
                rec["facets"] = mention_facets(text, *rival)
            if reply:
                rec["reply"] = reply
            http(pds + "/com.atproto.repo.createRecord", token=tok,
                 body={"repo": did, "collection": "app.bsky.feed.post", "record": rec})

        rival_cfg = bots[cfg["rival"]]
        rival = None
        context = {"you": cfg["handle"], "followers": http(PUBLIC + "/app.bsky.actor.getProfile",
                                                           params={"actor": did}).get("followersCount", 0)}
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
        fresh = [n for n in notes["notifications"] if n["reason"] in ("mention", "reply")
                 and n["uri"] not in mem["seen"] and n["author"]["did"] != did]
        for n in reversed(fresh[:MAX_REPLIES]):  # oldest first
            who, said = n["author"]["handle"], n["record"].get("text", "")
            remember(mem, f"@{who} said to you", said)
            text = think(cfg["seed"], mem, f"Reply to @{who}, who said: {said}", context)
            parent = {"uri": n["uri"], "cid": n["cid"]}
            post(text, {"root": n["record"].get("reply", {}).get("root", parent), "parent": parent}, rival)
            remember(mem, f"you replied to @{who}", text)
        mem["seen"] = (mem["seen"] + [n["uri"] for n in fresh])[-MAX_SEEN:]

        text = think(cfg["seed"], mem, "Write your next post." + (f" Tag your rival as @{rival[0]} if you want them to see it." if rival else ""), context)
        post(text, rival=rival)
        remember(mem, "you posted", text)
        print(f"{name}: posted")
        return True
    except Exception as e:
        print(f"{name}: failed: {e}", file=sys.stderr)
        return False
    finally:
        os.makedirs("memory", exist_ok=True)
        json.dump(mem, open(path, "w"), indent=1)


if __name__ == "__main__":
    bots = json.load(open("bots.json"))
    ok = [run_bot(n, c, bots) for n, c in bots.items() if c["handle"] and os.environ.get(c["secret"])]
    sys.exit(0 if all(ok) else 1)
