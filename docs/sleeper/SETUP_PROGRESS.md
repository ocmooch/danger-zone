# Vault setup — progress tracker

Resume point for the one-time setup in `05_VAULT_OFFSITE_SYNC.md` §9, done step by step with the
user. **Next session: read this file, re-check the "Verify" command for the first unchecked step,
then continue from there.** Never record secrets here (no private keys, passphrases, or where the
paper key is kept).

| # | Step | Who / where | Verify (Claude runs) | Status |
|---|------|-------------|----------------------|--------|
| 1 | `sudo apt install -y age rclone` | user, Ubuntu terminal (or `! ` in Claude) | `which age age-keygen rclone` | ☑ 2026-11-06 (age 1.1.1, rclone 1.60.1) |
| 2 | Key ceremony (§8.3) in a **separate** terminal; send Claude only the two `age1…` public keys | user, plain Ubuntu terminal | `ls ~/vault-keys` → only `primary.key.age`; keys written to `scripts/vault/recipients.txt` | ☑ 2026-11-07 |
| 3 | `rclone config` → remote `gdrive-vault`, type `drive`, blank client ID, scope `drive.file`, auto config yes, no shared drive | user, Ubuntu terminal | `rclone listremotes` shows `gdrive-vault:`; `rclone lsd gdrive-vault:` succeeds | ☑ 2026-11-07 |
| 4 | iCloud for Windows: sign in, enable iCloud Drive, create `iCloudDrive\ff-legacy-vault`, **Always keep on this device** | user, Windows | `ls /mnt/c/Users/MainUser/iCloudDrive/ff-legacy-vault` | ☑ 2026-11-07 |
| 5 | `ff-legacy-vault` repo is PRIVATE | — | `gh repo view ocmooch/ff-legacy-vault --json visibility` | ☑ 2026-11-05 |
| 6 | Register scheduled task (§5) — **after S0 builds `scripts/vault/vault_sync.sh`** | user, Windows PowerShell | `schtasks.exe /Query /TN dz-vault-sync` | ☑ 2026-11-07 |

## Open confirmations (non-secret)

- [x] D-3: `sleeper_user_09` → Owner 04 — confirmed 2026-11-06
- [x] D-3: `sleeper_user_01` → Owner 03 — confirmed 2026-11-06
- [x] Primary key + passphrase saved in Apple Passwords (armored, in Notes) — confirmed 2026-11-07
- [x] Paper recovery key stored physically separate — confirmed 2026-11-07
- [ ] D-10 scoring: stacked bonuses + FG 3 vs 5 at 50+ — **waits for 2026 data load**, not a setup step

## S0 — Legacy Seal v1 (built 2026-11-07, branch `feature/legacy-seal-v1`)

| Task | Status |
|------|--------|
| Pre-flight: integrity_check ok, foreign_key_check 0, alembic `d4e6f8a0b2c4` | ☑ |
| Seal built → `~/ff-legacy-vault/seal-v1-2026-11-07/`, verify.py PASSED, read-only | ☑ |
| Encrypted `dz-vault_seal-v1_20261107_5ade3104.tar.gz.age` (22.5 MB) | ☑ |
| Uploaded + hash-verified: Google Drive, iCloud, GitHub release | ☑ |
| Drive + GitHub copies downloaded back to `~/ff-legacy-vault/drill/`, sha256 identical | ☑ |
| **Decrypt test, primary key** — user runs `scripts/vault/restore_check.sh primary ~/ff-legacy-vault/drill/gdrive/dz-vault_seal-v1_20261107_5ade3104.tar.gz.age` | ☑ 2026-11-07 |
| **Decrypt test, recovery key** — user runs `scripts/vault/restore_check.sh recovery ~/ff-legacy-vault/drill/github/dz-vault_seal-v1_20261107_5ade3104.tar.gz.age` | ☑ 2026-11-07 |
| Manifest committed: `docs/sleeper/seals/seal-v1.manifest.json` (sha256 `d11c9ff0…8830`) | ☑ |
| 15 loose `data/*.bak*` → `data/backups/historical/` (none deleted); prune skip tested | ☑ |
| RUNBOOK "Legacy vault" section | ☑ |
| `MANIFEST.json` + `verify.py` in the private vault repo | PR on `ocmooch/ff-legacy-vault` |

After both decrypt tests pass → step 6 (scheduled task), then delete `~/ff-legacy-vault/drill/`.

## Log

- 2026-11-06 — tracker created; verified nothing installed yet (no age/rclone, no iCloudDrive, no ~/vault-keys).
- 2026-11-06 — step 1 blocked: always-on OS VPN breaks WSL DNS (apt + curl "Temporary failure resolving"). Created `C:\Users\MainUser\.wslconfig` (networkingMode=mirrored, dnsTunneling=true, autoProxy=true); user to run `wsl --shutdown` from PowerShell and reopen. On resume: test `getent hosts archive.ubuntu.com` + `curl -sI https://downloads.rclone.org/version.txt`, then retry step 1. Fallback if VPN still blocks: user-local binaries in ~/.local/bin (no sudo). Undo = delete .wslconfig.
- 2026-11-06 — resumed. Host is **Windows 10**: `networkingMode=mirrored` / `dnsTunneling` are Win11-only, so `.wslconfig` is inert (safe to delete). DNS + HTTPS now work anyway (`getent hosts archive.ubuntu.com`, rclone.org, apt mirror all OK) — VPN not blocking at present. If it breaks again with VPN on: static `/etc/resolv.conf` (`generateResolvConf=false`) pointing at the VPN's DNS, or the ~/.local/bin no-sudo fallback. Retrying step 1.
- 2026-11-06 — WSL DNS pinned to PIA resolver 10.0.0.243 (fallback 8.8.8.8) via static /etc/resolv.conf + wsl.conf generateResolvConf=false; verified. Proceeding to step 1.
- 2026-11-06 — step 1 done + verified; D-3 owner mappings confirmed by user. Next: step 2 key ceremony.
- 2026-11-06 — user's only password manager = Apple Passwords (iOS). Plan change: do step 4 (iCloud for Windows → includes iCloud Passwords app) BEFORE step 2; ceremony uses `age -p -a` (ASCII-armored primary.key.age, pasted into the entry's Notes — Passwords has no attachments) and age's auto-generated passphrase. Update §8.3 in S0.
- 2026-11-07 — step 4 verified (iCloudDrive/ff-legacy-vault exists). Step 2 verified: ~/vault-keys (700) holds only armored primary.key.age, no secret keys on disk; both public keys validated + written to scripts/vault/recipients.txt. Pending user confirms: primary saved in Apple Passwords, paper key stored. Next: step 3 rclone.
- 2026-11-07 — step 3 verified (gdrive-vault: type drive, scope drive.file, lsd OK). Key-storage confirmations done. **Steps 1–5 complete → next: build S0** (seal + sync scripts, decrypt test with each key, first verified uploads), then step 6.
- 2026-11-07 — **S0 built.** Seal-v1 sealed locally, encrypted, uploaded to all three destinations, each hash-verified; cloud copies downloaded back and identical. Archive name changed to `.tar.gz.age` (gzip inside); §8.3 updated for armored primary in Apple Passwords. Waiting on the user's two decrypt tests (restore_check.sh, separate terminal), then step 6.
- 2026-11-07 — **both decrypt tests PASSED** (primary via passphrase on the Drive copy; recovery via paper key on the GitHub copy). Note: recovery mode needs the `AGE-SECRET-KEY-1…` line — pasting an `age1…` public key gives "unknown identity type"; `age-keygen -y` confirms a paper key matches the recovery recipient. Next: step 6 (scheduled task), then delete `~/ff-legacy-vault/drill/`.
- 2026-11-07 — **step 6 done.** `dz-vault-sync` registered (weekly Sun 03:00, StartWhenAvailable); manual `schtasks /Run` → Last Result 0: seal skipped as already present everywhere, first weekly snapshot `dz-vault_snap_20261107_8040d8b8` uploaded + verified to gdrive/icloud/github. `~/ff-legacy-vault/drill/` deleted. **Vault setup complete.** Remaining: `ff-legacy-vault` MANIFEST/verify.py PR; D-10 waits on 2026 data.
- 2026-11-09 — **Evergreen fallback copy placed** (05 §10). The verified seal-v1 archive (sha256 `5ade3104…d737`) copied into a separate `ff-legacy-evergreen` folder on the local Windows disk, iCloud Drive, and Google Drive, each hash-verified, with README + .sha256. Locked: NTFS deny-delete ACLs (full write-lock locally, delete-lock on iCloud), Drive content lock + `--immutable`. Outside the sync job's folders, so retention can never touch it.
- 2026-11-09 — **Evergreen loud alert added** (05 §10): weekly job now ends with `check-evergreen` (presence, exact file set, size, sha256, read-only + deny-ACL locks). Failure → modal Windows popup + Desktop `VAULT-EVERGREEN-ALERT.txt` + non-zero exit. Live-tested: real check passes on all 3; a simulated missing folder raised the popup and the file, and the next passing run cleared it.
