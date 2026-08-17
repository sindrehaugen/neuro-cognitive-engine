> **Status:** shipped · **Verified-against:** 7304330 (main) · **Last-audited:** 2026-08-17

# DELETION STAGING NOTE: docs/RECON.md

**Unit:** U01  
**Target File:** `docs/RECON.md`  
**Action:** `git rm docs/RECON.md`  
**Status:** Staged for unpublishing / deletion on merge · **Verified-against:** 7304330  

---

### Rationale & Audit Evidence
1. **Orphaned Asset:** Not referenced in `_sidebar.md`, `_navbar.md`, or any documentation index.
2. **Zero Inbound References:** `git grep -n "RECON.md" 7304330` returns 0 hits across all files in the codebase.
3. **Information Disclosure Hazard:** Serves as a static asset on public GitHub Pages site, exposing:
   - Secret read locations across `nce/config.py` (`NCE_MASTER_KEY`, `PG_DSN`, `REDIS_URL`, `MINIO_SECRET_KEY`, etc.)
   - Key unwrapping and signing key retrieval mechanics in `nce/signing.py`
   - §4d list of unmitigated weaknesses in key rotation
4. **Merge Command:**
   ```bash
   git -C "<repo>" rm docs/RECON.md
   ```
