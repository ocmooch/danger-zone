# Restore drills (S9-D7)

Quarterly and before each release. Two kinds:

- **Key drill (you):** `scripts/vault/restore_check.sh primary <newest archive>` in a plain
  terminal (decrypts with your key, runs the archive's `verify.py`, deletes the scratch copy).
  Paste its result line here.
- **Local drill (key-free):** `uv run python scripts/vault/local_restore_drill.py`: restores
  the newest plaintext seal directory into scratch, `migrate up`, re-adopts the committed
  manifest + `legacy verify`, and runs a dashboard BFF smoke against the copy.

| Date | Kind | Source | Result |
|---|---|---|---|
| 2026-11-10 | local drill | seal-v1-2026-11-07 | integrity ok; migrate up ok; PASSED (full): live DB matches seal-v1; smoke ok: 16 seasons, 1 box score |
| _pending_ | key drill (you) | newest `dz-vault_*` archive | run `restore_check.sh primary <archive>` and add the line |
