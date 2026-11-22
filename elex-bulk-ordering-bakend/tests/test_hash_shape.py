"""A stored password hash is ours only at the full digest width (audit F-42)."""

from app.core.security import hash_password, is_password_hash, parse_hash, verify_password


def test_a_full_hash_round_trips():
    stored = hash_password("correct horse battery staple", iterations=1000)
    assert is_password_hash(stored)
    assert verify_password("correct horse battery staple", stored)
    assert not verify_password("wrong", stored)


def test_a_digest_cut_short_is_not_a_hash_of_ours():
    stored = hash_password("correct horse battery staple", iterations=1000)
    algo, iterations, salt, digest = stored.split("$")
    truncated = "$".join([algo, iterations, salt, digest[:8]])
    assert parse_hash(truncated) is None
    assert not is_password_hash(truncated)
    assert not verify_password("correct horse battery staple", truncated)
