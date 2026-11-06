export const now = () => new Date().toISOString();

/** Hours since an ISO time; huge if never. */
export const ageH = (at) => (at ? (Date.now() - Date.parse(at)) / 3.6e6 : 1e9);

// shortcut: counts UTF-16 units, not graphemes; fine unless posts get emoji-heavy
export function clip(text, n = 300) {
  text = String(text).split(/\s+/).filter(Boolean).join(" ");
  return text.length <= n ? text : text.slice(0, n - 1).trimEnd() + "…";
}

/** Make @handle in the text a real mention so the rival gets notified. */
export function mentionFacets(text, handle, did) {
  const tag = "@" + handle;
  const i = text.indexOf(tag);
  if (i < 0) return [];
  const enc = new TextEncoder();
  const start = enc.encode(text.slice(0, i)).length;
  return [{
    index: { byteStart: start, byteEnd: start + enc.encode(tag).length },
    features: [{ $type: "app.bsky.richtext.facet#mention", did }],
  }];
}

/** Pull the {"text","note"} object out of a model reply, even if it's wrapped in fences or chatter. */
export function parseReply(raw) {
  const s = String(raw ?? "");
  return JSON.parse(s.slice(s.indexOf("{"), s.lastIndexOf("}") + 1));
}
