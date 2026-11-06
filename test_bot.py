from bot import age_h, clip, mention_facets

assert clip("a  b\n c") == "a b c"
assert len(clip("x" * 500)) == 300
f = mention_facets("yo é @kev.bsky.social pay up", "kev.bsky.social", "did:x")[0]["index"]
assert "yo é @kev.bsky.social pay up".encode()[f["byteStart"]:f["byteEnd"]] == b"@kev.bsky.social"
assert mention_facets("no tag", "kev.bsky.social", "did:x") == []
assert age_h(None) > 1e8 and 0.9 < age_h("2000-01-01T00:00:00Z") / (24 * 365 * 26) < 1.1
print("ok")
