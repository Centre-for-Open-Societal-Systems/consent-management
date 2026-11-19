"""A fixed TEST-ONLY Ed25519 private key for the sanity e2e.

This key exists SOLELY so the sanity suite can seed a persistent test partner in
Partner Management and then sign a consent object that verifies against it. It is
NOT secret and MUST NEVER be used by a real partner — the sanity partner is bound
to a dedicated test controller/audience and is inert for real data sharing.

Only the private key is stored: the public half (which is what PM stores) is
derived from it at seed time, so there is a single source of truth for the pair.

Override with SANITY_PM_PRIVATE_KEY_PEM (+ a matching PM-seeded key) in
environments where you'd rather manage your own test key.
"""

# Ed25519 PKCS#8 private key — TEST ONLY.
TEST_PRIVATE_KEY_PEM = """-----BEGIN PRIVATE KEY-----
MC4CAQAwBQYDK2VwBCIEIEzzrTrPbzYjc2k3BCPMcK6vaULPtmvxVo2EdKjSTW3a
-----END PRIVATE KEY-----
"""

# Stable identifiers for the persistent sanity partner. Its audience (the
# consent's `aud`) maps to its PM partner by convention: cm-sanity ->
# PARTNER_CM_SANITY.
DEFAULT_AUDIENCE = "cm-sanity"
DEFAULT_KID = "cm-sanity-1"
