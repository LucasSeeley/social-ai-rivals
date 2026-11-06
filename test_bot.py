from bot import clip, mention_facets

assert clip("a  b\n c") == "a b c"
assert len(clip("x" * 500)) == 300
f = mention_facets("yo é @kev.bsky.social pay up", "kev.bsky.social", "did:x")[0]["index"]
assert "yo é @kev.bsky.social pay up".encode()[f["byteStart"]:f["byteEnd"]] == b"@kev.bsky.social"
assert mention_facets("no tag", "kev.bsky.social", "did:x") == []
print("ok")
