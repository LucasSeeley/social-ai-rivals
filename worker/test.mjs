import assert from "node:assert";
import { ageH, clip, mentionFacets, parseReply } from "./util.js";

assert.equal(clip("a  b\n c"), "a b c");
assert.equal(clip("x".repeat(500)).length, 300);
const text = "yo é @kev.bsky.social pay up";
const { byteStart, byteEnd } = mentionFacets(text, "kev.bsky.social", "did:x")[0].index;
assert.equal(new TextDecoder().decode(new TextEncoder().encode(text).slice(byteStart, byteEnd)), "@kev.bsky.social");
assert.deepEqual(mentionFacets("no tag", "kev.bsky.social", "did:x"), []);
assert.ok(ageH(null) > 1e8);
assert.ok(Math.abs(ageH(new Date(Date.now() - 3.6e6).toISOString()) - 1) < 0.01);
assert.deepEqual(parseReply('```json\n{"text": "hi", "note": ""}\n```'), { text: "hi", note: "" });
console.log("ok");
