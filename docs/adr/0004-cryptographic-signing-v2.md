> **Status:** shipped · **Verified-against:** 7304330 (main) · **Last-audited:** 2026-08-17

# ADR-0004: Cryptographic Signing v2 — Argon2id Key Wrap + ML-DSA-44 + Merkle Chain Hash

## Status

Shipped

## Context

NCE positions itself as a system of truth: stored memories and event_log rows must be tamper-evident so any retrieval can detect post-write corruption. Three independent mechanisms are required:

1. **At-rest key wrapping** — signing keys stored in PostgreSQL must be encrypted so a database dump does not expose key material. The original implementation used a single SHA-256 digest of the master key as the AES wrapping key, which provides no key-stretching and is trivially brute-forced.
2. **Row signatures** — each `event_log` row needs an unforgeable signature over its canonical fields so individual rows can be verified without trusting the entire chain.
3. **Chain integrity** — even if individual rows are signed, an attacker who can delete rows or reorder them would not be detected. A Merkle chain hash threads all rows in sequence so any gap or reordering breaks verification.

The original v1 signing used HMAC-SHA256 with a symmetric key. This provides integrity but not non-repudiation, and does not provide post-quantum security.

## Decision

Three mechanisms shipped together in `nce/signing.py`:

**1. Versioned key-wrapping formats (Argon2id preferred)**

Signing keys are stored encrypted in the `signing_keys` table. The wrapping key is derived from `NCE_MASTER_KEY` using a per-blob random 16-byte salt plus one of three versioned KDFs:

- `v4` (magic `TC4\x01`): PBKDF2-HMAC-SHA256, 600,000 iterations (OWASP 2026).
- `v3` (magic `TC3\x01`): Argon2id — preferred when `argon2-cffi` is available; OWASP 2025 parameters: time_cost=3, memory_cost=65536 (64 MiB), parallelism=4.
- `v2` (magic `TC2\x01`): PBKDF2-HMAC-SHA256, 100,000 iterations (NIST minimum).
- `legacy` (no prefix): SHA-256 digest only — decryption supported for migration; no new writes.

New writes always use v3 (Argon2id) or v4. All wire formats append a 12-byte nonce and AES-256-GCM ciphertext+tag.

**2. ML-DSA-44 asymmetric signing (FIPS 204)**

New `event_log` rows are signed with ML-DSA-44 (CRYSTALS-Dilithium, 128-bit classical security, post-quantum). The canonical FIPS 204 parameter set name is "ML-DSA-44". The algorithm identifier `_KEY_ALGORITHM = "ML-DSA-44"` is stamped into each `signing_keys` row.

Existing HMAC-SHA256 rows remain verifiable; ML-DSA is the new write path for Lattice Signing. ML-DSA requires `cryptography>=44.0.0`; the code gracefully degrades (`_HAS_MLDSA = False`) if the library is older.

**3. Merkle chain hash**

Each `event_log` row carries a `chain_hash` column (BYTEA). The value is:

```
chain_hash = SHA-256(content_hash || previous_chain_hash)
```

where `content_hash` is SHA-256 of the canonical signing fields for the current row, and `previous_chain_hash` is the `chain_hash` of the immediately preceding row in the namespace (by `event_seq`). The first event uses a genesis sentinel of 32 zero bytes. This links all rows into a Merkle chain: any deletion, insertion, or reordering breaks the chain.

**Source citations** (verified via `git show main:<path>`):
- `nce/signing.py:13` — Argon2id and PBKDF2 key-wrapping described in module docstring
- `nce/signing.py:17` — `Wire format (v3 — Argon2id, preferred): b'TC3\x01' || salt (16) || nonce (12) || ciphertext+tag`
- `nce/signing.py:121-125` — magic prefix constants `_ENCRYPTED_KEY_BLOB_V2`, `_V3`, `_V4`
- `nce/signing.py:210-213` — Argon2id parameters: `_ARGON2_TIME_COST=3`, `_ARGON2_MEMORY_COST=65536`, `_ARGON2_PARALLELISM=4`, `_ARGON2_HASH_LEN=32`
- `nce/signing.py:132-137` — ML-DSA-44 section header; `_KEY_ALGORITHM: str = "ML-DSA-44"` — FIPS 204 parameter set
- `nce/signing.py:139-148` — `MLDSA44PrivateKey` / `MLDSA44PublicKey` import with `_HAS_MLDSA` fallback
- `nce/event_log.py:91` — `_GENESIS_SENTINEL: Final[bytes] = b"\x00" * 32` — genesis sentinel
- `nce/event_log.py:650-669` — `_compute_chain_hash`: `SHA-256(content_hash + previous_chain_hash)`
- `nce/event_log.py:672-704` — `_fetch_previous_chain_hash`: fetches prior row by `event_seq DESC`
- `nce/schema.sql:622-641` — `event_log` table: `chain_hash BYTEA`, `signature BYTEA NOT NULL`, `signature_version SMALLINT NOT NULL DEFAULT 1`

## Consequences

### Positive

- Argon2id wrapping makes offline brute-force of signing keys computationally expensive even with a GPU farm.
- ML-DSA-44 provides post-quantum asymmetric signing; HMAC-SHA256 rows remain verifiable during transition.
- The Merkle chain hash detects deletion, insertion, or reordering of any row in a namespace without requiring a full table scan — verification is sequential.
- Versioned wire formats (TC2/TC3/TC4) allow future KDF upgrades without breaking existing blobs.

### Negative / Trade-offs

- ML-DSA requires `cryptography>=44.0.0`; older deployments silently fall back to HMAC-SHA256 until the library is upgraded.
- Chain hash computation serialises `event_log` appends within a namespace (`_fetch_previous_chain_hash` must read the latest row inside the same transaction); this limits per-namespace write throughput.
- `chain_hash` is `NULL` for rows written before the column was added; chain verification must handle the pre-chain era explicitly.
- Key rotation (`rotate_key()`) zeros all cached buffers and clears the TTL cache, causing a brief spike in `signing_keys` table reads on the next writes.

### Seams (planned/in-flight)

- [planned] Deterministic UUID remapping on snapshot import (noted in `nce/snapshot_mcp_handlers.py`: "Reusing deterministic remap once Phase H lands") — until Phase H, restored snapshots generate fresh UUIDs and signatures.
