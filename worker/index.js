// Runs one bot per cron tick (they alternate): reply to new mentions, maybe post. Memory lives in D1.
import bots from "../bots.json";
import { ageH, clip, mentionFacets, now, parseReply } from "./util.js";

const PUBLIC = "https://public.api.bsky.app/xrpc";
const ENTRY = "https://bsky.social/xrpc";
const MODEL = "@cf/meta/llama-3.1-8b-instruct-fp8"; // small = many more replies per free daily allowance
const MAX_EVENTS = 60, MAX_JOURNAL = 40, MAX_SEEN = 300, MAX_REPLIES = 2;
const POST_EVERY_H = 3;  // new top-level post at most this often; replies happen every tick
const REPLY_AFTER_S = 90; // wait a bit before answering, so it feels human
const THREAD_CAP = 8;     // stop replying in one thread after this many replies (spam guard)

const RULES = `You post on Bluesky from a labeled bot account, playing your character.
Write like a real dumb guy posting, not like an AI: casual, short, lowercase is fine, no hashtags.
Your personality is NOT fixed. It grows only from what actually happens to you, which your journal records. Stay consistent with your journal and never repeat a past post.
Never harass or insult real people, and only mention a real person when replying to them. No hate, sexual content, drugs, politics, or anything dangerous. If someone sincerely asks, you can admit you're a bot.
Reply with JSON: {"text": "<the post, under 280 characters>", "note": "<one short line for your private journal: what happened, how you feel, or something you now believe. empty if nothing new>"}`;

async function http(url, { body, token, params } = {}) {
  if (params) url += "?" + new URLSearchParams(params);
  const headers = { "Content-Type": "application/json", Accept: "application/json", "User-Agent": "social-ai-rivals" };
  if (token) headers.Authorization = `Bearer ${token}`;
  const r = await fetch(url, body === undefined ? { headers } : { method: "POST", headers, body: JSON.stringify(body) });
  const t = await r.text();
  if (!r.ok) throw new Error(`${url.split("?")[0]} HTTP ${r.status}: ${t.slice(0, 300)}`);
  return JSON.parse(t);
}

async function log(env, sql, ...params) {
  try { await env.DB.prepare(sql).bind(...params).run(); } // the dashboard log must never stop a bot
  catch (e) { console.log("log skipped:", e.message); }
}

const remember = (mem, kind, text) => { mem.events = [...mem.events, { at: now(), kind, text }].slice(-MAX_EVENTS); };

async function think(env, name, seed, mem, task, situation) {
  const out = await env.AI.run(MODEL, {
    messages: [
      { role: "system", content: seed + "\n\n" + RULES },
      { role: "user", content: JSON.stringify({ situation, journal: mem.journal, recent_events: mem.events, task }) },
    ],
  });
  const reply = typeof out.response === "object" ? out.response : parseReply(out.response);
  const note = String(reply.note ?? "").trim();
  if (note) {
    mem.journal = [...mem.journal, note].slice(-MAX_JOURNAL);
    await log(env, "INSERT INTO journal (bot, at, note) VALUES (?, ?, ?)", name, now(), note);
  }
  return clip(reply.text);
}

async function runBot(name, env) {
  const cfg = bots[name];
  const row = await env.DB.prepare("SELECT data FROM mem WHERE bot = ?").bind(name).first();
  const mem = row ? JSON.parse(row.data) : { journal: [], events: [], seen: [] };
  try {
    // shortcut: a fresh login each tick (Bluesky allows 300/day per account); cache the session if ticks get faster than ~6 min per bot
    const s = await http(ENTRY + "/com.atproto.server.createSession", { body: { identifier: cfg.handle, password: env[cfg.secret] } });
    const pds = (s.didDoc?.service?.find((x) => x.id.endsWith("atproto_pds"))?.serviceEndpoint ?? "https://bsky.social") + "/xrpc";
    const tok = s.accessJwt, did = s.did;

    const post = async (text, reply, rival, toWho) => {
      const rec = { $type: "app.bsky.feed.post", text, createdAt: now() };
      if (rival) rec.facets = mentionFacets(text, ...rival);
      if (reply) rec.reply = reply;
      const { uri } = await http(pds + "/com.atproto.repo.createRecord", { token: tok, body: { repo: did, collection: "app.bsky.feed.post", record: rec } });
      await log(env, "INSERT INTO posts (bot, at, kind, text, to_who, uri) VALUES (?, ?, ?, ?, ?, ?)",
        name, rec.createdAt, reply ? "reply" : "post", text, toWho ?? null, uri);
    };

    const me = await http(PUBLIC + "/app.bsky.actor.getProfile", { params: { actor: did } });
    const situation = { you: cfg.handle, followers: me.followersCount ?? 0 };
    const last = mem.last_stats ?? {};
    if (last.n !== situation.followers || ageH(last.at) >= 1) {
      await log(env, "INSERT OR REPLACE INTO stats (bot, at, followers) VALUES (?, ?, ?)", name, now(), situation.followers);
      mem.last_stats = { n: situation.followers, at: now() };
    }
    const rivalCfg = bots[cfg.rival];
    let rival = null;
    if (rivalCfg.handle) {
      const rp = await http(PUBLIC + "/app.bsky.actor.getProfile", { params: { actor: rivalCfg.handle } });
      rival = [rivalCfg.handle, rp.did];
      const { feed } = await http(PUBLIC + "/app.bsky.feed.getAuthorFeed", { params: { actor: rp.did, limit: 5, filter: "posts_no_replies" } });
      situation.rival = { handle: rivalCfg.handle, followers: rp.followersCount ?? 0, their_recent_posts: feed.map((f) => f.post.record.text) };
    } else {
      situation.rival = "hasn't shown up on Bluesky yet";
    }

    // Bluesky bot rules: only interact with people who tagged or replied to us.
    const { notifications } = await http(pds + "/app.bsky.notification.listNotifications", { token: tok, params: { limit: 30 } });
    const threads = (mem.threads ??= {});
    const fresh = notifications.filter((n) => ["mention", "reply"].includes(n.reason) && !mem.seen.includes(n.uri)
      && n.author.did !== did && ageH(n.indexedAt) * 3600 >= REPLY_AFTER_S);
    const done = [];
    for (const n of fresh.slice(-MAX_REPLIES).reverse()) { // oldest first
      done.push(n.uri);
      const parent = { uri: n.uri, cid: n.cid };
      const root = n.record?.reply?.root ?? parent;
      if ((threads[root.uri] ?? 0) >= THREAD_CAP) continue;
      threads[root.uri] = (threads[root.uri] ?? 0) + 1;
      const who = n.author.handle, said = n.record?.text ?? "";
      remember(mem, `@${who} said to you`, said);
      const text = await think(env, name, cfg.seed, mem, `Reply to @${who}, who said: ${said}`, situation);
      await post(text, { root, parent }, rival, who);
      remember(mem, `you replied to @${who}`, text);
    }
    mem.seen = [...mem.seen, ...done].slice(-MAX_SEEN);
    mem.threads = Object.fromEntries(Object.entries(threads).slice(-100));

    if (ageH(mem.last_post) >= POST_EVERY_H) {
      const tag = rival ? ` Tag your rival as @${rival[0]} if you want them to see it.` : "";
      const text = await think(env, name, cfg.seed, mem, "Write your next post." + tag, situation);
      await post(text, null, rival);
      remember(mem, "you posted", text);
      mem.last_post = now();
    }
    console.log(`${name}: replied to ${done.length}`);
  } finally {
    await env.DB.prepare("INSERT OR REPLACE INTO mem (bot, data) VALUES (?, ?)").bind(name, JSON.stringify(mem)).run();
  }
}

export default {
  async scheduled(event, env, ctx) {
    const live = Object.keys(bots).filter((n) => bots[n].handle && env[bots[n].secret]);
    if (!live.length) return;
    const tick = Math.floor(event.scheduledTime / 180000); // one bot per 3-minute tick, alternating
    ctx.waitUntil(runBot(live[tick % live.length], env));
  },
};
