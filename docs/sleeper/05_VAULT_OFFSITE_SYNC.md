# 05 — Vault Off-site Sync and Encryption Keys

**Status:** designed 2026-11-05; built + first sync verified 2026-11-07 (S0) — `scripts/vault/` · **Repo:** `danger-zone`
**Parent:** `03_LEGACY_PRESERVATION.md` §4 (vault) · **Decision:** D-7

This covers how encrypted vault archives get from this machine to Google Drive, iCloud Drive and the
private `ocmooch/ff-legacy-vault` repo automatically, how old copies are rotated out, and the one
decision only the user can make: **the encryption keys and passphrase.**

---

## 1. Design in one picture

```
 producers                          ~/ff-legacy-vault/
 ─────────                          ├── seals/seal-v1-…/           (plain, read-only, local only)
 legacy seal / amend   ─┐           ├── outbox/                    (encrypted archives ready to ship)
 weekly snapshot job   ─┼─► tar ─► age (public keys only) ─► outbox/dz-vault_<kind>_<date>_<sha8>.tar.gz.age
                        │                                         │
                        │              vault_sync (weekly + after every seal)
                        │      ┌──────────────────┬───────────────┴──────────┬────────────────────────┐
                        │      ▼                  ▼                          ▼                        │
                        │  Google Drive        iCloud Drive              GitHub private repo           │
                        │  rclone `drive`      iCloud for Windows        `gh release` asset            │
                        │  (drive.file scope)  synced folder on C:       ocmooch/ff-legacy-vault       │
                        │      └──────── verify hash ─► retention: keep seal-v1 + 4 newest ──────────┘
                        └─► sync-status.json (last success per destination; pipeline warns if stale)
```

- **Encryption happens once, locally, with public keys only.** The machine never holds anything that
  can decrypt the archives, so the scheduled job needs no secret and a stolen laptop or a leaked
  cloud account exposes nothing readable.
- **Every destination receives the same bytes** (same file name, same sha256), so any one copy can
  be checked against the manifest.
- **Archives are small:** the whole live DB compresses to ~24 MB; one archive (DB + assets + seeds +
  raw Sleeper payloads) is expected at 25–40 MB. Five copies per destination ≈ 200 MB.

## 2. What gets shipped

| Kind | Name | Produced by | Contents |
|------|------|-------------|----------|
| Seal | `dz-vault_seal-v1_20261106_<sha8>.tar.gz.age` | `legacy seal` / `legacy amend` | the full seal directory (`03` §4.1) |
| Snapshot | `dz-vault_snap_20261108_<sha8>.tar.gz.age` | weekly job | online backup of the live DB + `data/assets/` + seeds + `data/raw/sleeper/` + manifest |

Snapshots carry the growing Sleeper era between seals; seals are the certified checkpoints.

## 3. Retention: at most 5 per destination

Per destination, after a verified upload:

1. **Pin `seal-v1` forever.** It is the certified original of the NFL.com era. If a later problem
   ever crept into the live DB, every newer archive would carry it; seal-v1 is the copy that can't
   rotate away. (≈ 25 MB.)
2. Keep the **4 newest** other archives (seals or snapshots, by date in the name).
3. Delete the rest.

Result: never more than 5 files per destination. Deletions are soft for 30 days on Drive (Trash)
and iCloud (Recently Deleted), which is a free extra safety net; both empty themselves.
Retention only ever runs **after** the new archive has been uploaded and its hash verified at that
destination, so a failed upload can't leave a destination with fewer good copies.

## 4. Destinations

### 4.1 Google Drive — `rclone`

- Tool: `rclone` `drive` backend (Ubuntu's `apt` 1.60 is fine for Drive).
- Remote `gdrive-vault`, scope **`drive.file`**: rclone can see and delete only files it created —
  not the rest of the user's Drive.
- Folder: `ff-legacy-vault/` in My Drive.
- Verify: compare `rclone md5sum` of the uploaded file to the local file's md5.
- Reliability note: use rclone's built-in client ID (fine at this volume). If you create your own
  Google OAuth client instead, **publish it to "In production"** — clients left in "Testing" get
  refresh tokens that expire after 7 days, which silently breaks unattended uploads.

### 4.2 iCloud Drive — iCloud for Windows synced folder

- Tool: **iCloud for Windows** (Microsoft Store), signed in to the user's Apple ID, with iCloud Drive
  enabled. It syncs `C:\Users\MainUser\iCloudDrive\`, which WSL sees as
  `/mnt/c/Users/MainUser/iCloudDrive/`.
- The job copies the archive into `iCloudDrive/ff-legacy-vault/`; the Windows app uploads it, and
  deleting an old file locally deletes it in iCloud.
- Why not rclone's `iclouddrive` backend: it needs rclone ≥ 1.69 (not in Ubuntu 24.04), its Apple
  session token expires roughly monthly and needs a fresh 2FA login, and it doesn't work with
  Advanced Data Protection. The Windows app has none of those problems.
- In Windows Explorer, right-click the `ff-legacy-vault` folder → **Always keep on this device**, so
  the archive stays local *and* in iCloud.
- Verify: local hash of the copied file. iCloud upload completion isn't observable from WSL, so the
  monthly drill (§7) downloads one archive from icloud.com and checks it.

### 4.3 GitHub — private `ocmooch/ff-legacy-vault`

- Tool: `gh` (already authenticated as `ocmooch`).
- One release per archive (tag = file stem), archive as the release asset; `MANIFEST.json` and
  `verify.py` committed in the repo in plain text (hashes and code only).
- Verify: compare the asset's sha256 `digest` from the GitHub API to the local hash.
- Retention: `gh release delete <tag> --cleanup-tag --yes` for releases beyond the policy.
- The repo must stay **private**; the sync script refuses to upload if
  `gh repo view --json visibility` isn't `PRIVATE`.

## 5. Scheduling

WSL shuts down when nothing is running, so timers inside WSL can't be relied on. Windows Task
Scheduler starts the job instead:

```powershell
# Run once in Windows PowerShell (not as admin). Weekly, Wednesday 07:00 (an hour after the
# 06:00 capture, so the snapshot includes that week's data); runs at next login if missed.
# Runs from the main copy (~/danger-zone-main); the weekly run refuses any branch but main,
# and waits for a running weekly capture (shared lock) before snapshotting.
$action  = New-ScheduledTaskAction -Execute "wsl.exe" `
           -Argument "-d Ubuntu -u mainuser -- /home/mainuser/danger-zone-main/scripts/vault/vault_sync.sh"
$trigger = New-ScheduledTaskTrigger -Weekly -DaysOfWeek Wednesday -At 7am
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -RunOnlyIfNetworkAvailable `
            -ExecutionTimeLimit (New-TimeSpan -Hours 1)
Register-ScheduledTask -TaskName "dz-vault-sync" -Action $action -Trigger $trigger -Settings $settings
```

Then set the window mode with `scripts/windows/task_windows.sh hidden|visible` (RUNBOOK "Window mode").

`legacy seal` and `legacy amend` also call the sync immediately after producing a seal.

## 6. Failure handling and monitoring

- Each destination is independent: one failing doesn't stop the others; the job exits non-zero if
  any destination failed (visible in Task Scheduler's "Last Run Result").
- `~/ff-legacy-vault/sync-status.json` records per destination: last success time, file, hash.
- `ff-pipeline status` and the pipeline preflight **warn** when any destination's last success is
  older than 10 days, and `legacy verify` reports it.
- Logs: `data/logs/vault_sync-<date>.log` (no secrets, no member names).
- Upload uses temporary names and renames on success where the destination supports it (Drive,
  local folder); a half-written archive never counts as a copy.

## 7. Restore drill (monthly while in season, quarterly otherwise)

1. Download the newest archive from **one** destination (rotate: Drive → iCloud → GitHub).
2. `scripts/vault/restore_check.sh primary|recovery <archive>` — decrypts to a temp dir, runs
   `verify.py`, deletes the temp dir. (By hand: `age -d -i <identity> -o a.tar.gz <archive>`,
   `tar -xzf a.tar.gz -C <scratch>`, `python3 <scratch>/<dir>/verify.py`.)
3. (included in step 2)
4. Record the result in `sync-status.json` (`last_drill`).

## 8. The encryption decision (needed before S0 runs)

### 8.1 Recommended scheme: two age key pairs, passphrase-protected

Use **`age` public-key encryption**, not a single shared passphrase:

| Item | What it is | Where it lives |
|------|-----------|----------------|
| Primary recipient (public key) | `age1…` string | in the repo config `scripts/vault/recipients.txt` — public, safe to commit |
| Recovery recipient (public key) | `age1…` string | same file |
| Primary identity (private key) | `AGE-SECRET-KEY-1…`, **itself encrypted with your passphrase** (`primary.key.age`) | your password manager (as an attachment) + one offline copy |
| Recovery identity (private key) | a second private key, printed on paper | somewhere physically separate (safe, or with someone you trust) |
| Passphrase | protects the primary identity file | your password manager **and** written down with the paper recovery key |

Every archive is encrypted to **both** public keys, so either private key can open it. Losing one key
or forgetting the passphrase is survivable; losing both is not.

Why this over a plain passphrase: the scheduled job can encrypt without any secret on this machine;
the secret is only needed on the rare day you restore. A plain passphrase would have to sit on disk
for the weekly job to use it.

### 8.2 Choosing the passphrase

- Let your password manager generate a **6-word passphrase** (diceware-style, ≈ 77 bits). Words are
  far easier to type correctly in a stressful restore than random symbols.
- Use it **only** for this. Don't reuse your Apple, Google or GitHub passwords — those accounts hold
  the copies.
- Never put it in `.env`, a repo, a cloud folder next to the archives, or a chat with Claude.

### 8.3 Key ceremony (do this yourself, in a separate terminal — not through Claude)

Running these in this Claude session would put the private key into the conversation, so use a
plain Ubuntu terminal:

```bash
sudo apt install -y age
mkdir -p ~/vault-keys && chmod 700 ~/vault-keys && cd ~/vault-keys

# Primary key: generated, then wrapped with a passphrase (age suggests one). ASCII-armored (-a)
# so the whole file can be pasted as text into a password-manager note.
age-keygen -o primary.key && age -p -a -o primary.key.age primary.key
grep 'public key' primary.key        # copy this age1… line
shred -u primary.key                 # keep only the passphrase-protected copy

# Recovery key: print or hand-write it, then destroy the file.
age-keygen -o recovery.key
grep 'public key' recovery.key       # copy this age1… line
cat recovery.key                     # write the AGE-SECRET-KEY-1… line on paper (or print it)
shred -u recovery.key
```

Then:

1. Paste the armored `primary.key.age` text into your password manager entry (as done 2026-11-07:
   Apple Passwords, in the entry's Notes — it has no attachments) and save the passphrase there.
2. Store the paper recovery key (with the passphrase written on it) somewhere physically safe.
3. Give Claude **only the two `age1…` public keys**. They go into `scripts/vault/recipients.txt`.
4. S0 runs a decrypt test with each key (`scripts/vault/restore_check.sh`) before the first upload
   is trusted.

## 9. One-time setup checklist (user)

| # | Step | Where |
|---|------|-------|
| 1 | `sudo apt install -y age rclone` | Ubuntu terminal |
| 2 | Key ceremony (§8.3); send Claude the two public keys | Ubuntu terminal |
| 3 | `rclone config` → new remote `gdrive-vault` → type `drive` → leave client ID blank → scope **`drive.file`** → auto config **yes** (the sign-in page opens in your Windows browser) → no shared drive | Ubuntu terminal |
| 4 | Install **iCloud for Windows** from the Microsoft Store; sign in; enable iCloud Drive; create `iCloudDrive\ff-legacy-vault`; set it to **Always keep on this device** | Windows |
| 5 | Confirm `gh repo view ocmooch/ff-legacy-vault` shows `PRIVATE` (✅ checked 2026-11-05) | — |
| 6 | Register the scheduled task (§5) | Windows PowerShell |

Claude can do everything else (scripts, tests, the first seal, the first sync and its verification)
once steps 1–4 are done.

## 10. Evergreen fallback copy (placed 2026-11-09)

One copy of the verified seal-v1 archive is kept **permanently, outside the sync job**, in each of
three places. It is the last-resort source of data if every rotating copy is lost or compromised. It
is never overwritten, renamed, re-encrypted, or replaced, including by later seals.

| | |
|---|---|
| File | `dz-vault_seal-v1_20261107_5ade3104.tar.gz.age` (22,481,806 bytes) |
| sha256 | `5ade31049ee588f701651753f8d71758bb054ad1f4a61c8bbab32bdea489d737` |
| Contents | seal-v1: full SQLite DB (2010–2025) + CSV/JSONL exports + seeds/assets + `verify.py` |
| Decrypt-tested | primary and recovery keys, 2026-11-07 (§9 log in `SETUP_PROGRESS.md`) |

Each folder also holds `README.txt` (what this is, how to restore) and `<file>.sha256`.

| Location | Path | Protection |
|----------|------|-----------|
| Local (Windows disk, survives a WSL reset) | `C:\Users\MainUser\ff-legacy-evergreen\` | Read-only attribute; NTFS deny-ACE for Everyone on each file (delete, write, append, write attributes) and on the folder (delete, delete-child). Delete, edit, rename and folder removal all tested → "Permission denied". |
| iCloud Drive | `iCloudDrive\ff-legacy-evergreen\` | Read-only attribute; NTFS deny-ACE for Everyone: delete on each file, delete + delete-child on the folder. Lighter than local so the iCloud sync client can still manage the placeholder. Blocks deletion from this PC only; a deletion on another Apple device or iCloud.com would go to iCloud's 30-day Recently Deleted. |
| Google Drive | `gdrive-vault:ff-legacy-evergreen/` | Uploaded with `rclone --immutable`; each file **locked** (Drive `contentRestrictions.readOnly`), so content can't be changed or replaced. Personal Drive can't make a file undeletable, so the safety net there is the 30-day Trash. |

**Why these are safe from automation:** `vault_sync.py` only lists and prunes
`gdrive-vault:ff-legacy-vault`, `iCloudDrive\ff-legacy-vault` and the GitHub releases. It never sees
the `ff-legacy-evergreen` folders. Keep it that way: never point a destination or retention rule at them.

**Loud alert (automatic, weekly).** Every weekly `dz-vault-sync` run ends with
`vault_sync.sh check-evergreen`, which runs even if the snapshot or sync failed. It is read-only and
checks each location for: folder present; exactly the three expected files (missing *or* extra files
flag); archive size + sha256; and, on the two Windows folders, that the read-only attribute and the
deny-ACL are still in place. Drive's content lock can't be read with rclone 1.60, so the hash check
covers Drive. Any problem →

1. a **modal Windows popup** ("FF VAULT ALERT", stays on top until dismissed),
2. `C:\Users\MainUser\Desktop\VAULT-EVERGREEN-ALERT.txt` listing every problem + what to do,
3. ERROR lines in `data/logs/vault_sync-<date>.log`, `"evergreen": {"ok": false, …}` in
   `sync-status.json`, and a non-zero Task Scheduler "Last Run Result".

The Desktop file is removed automatically once a later check passes. Run it any time (e.g. after AI-
assisted work near the vault): `~/danger-zone/scripts/vault/vault_sync.sh check-evergreen`. A unit
test fails if a sync destination is ever pointed at an evergreen folder.

**Deliberately removing a lock** (only if the user decides to retire a copy): Windows:
`icacls "<path>" /remove:d *S-1-1-0` then `attrib -R "<path>"` (the owner can always edit the ACL).
Drive: open the file → ⋮ → File information → Unlock, or PATCH `contentRestrictions: [{readOnly:false}]`.
