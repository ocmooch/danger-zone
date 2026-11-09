#!/usr/bin/env python3
"""Encrypt vault archives and ship them off-machine. Standard library only.

    vault_sync.sh                     # weekly job: snapshot + sync (what Task Scheduler runs)
    vault_sync.sh pack-seal <dir>     # encrypt a sealed directory into the outbox
    vault_sync.sh snapshot            # snapshot the live DB into the outbox
    vault_sync.sh sync                # ship the outbox to every destination
    vault_sync.sh check-evergreen     # verify the permanent fallback copies (alerts loudly)
    vault_sync.sh status              # print sync-status.json

Encryption uses only the PUBLIC keys in recipients.txt, so this job holds no
secret. Each destination is independent; the exit code is non-zero if any
destination failed. Retention (per destination, after a verified upload):
keep seal-v1 forever + the 4 newest other archives. Design:
docs/sleeper/05_VAULT_OFFSITE_SYNC.md.

The weekly job also checks the evergreen fallback copies (05 §10). They live in
separate folders this job never writes to; any missing, changed, extra or
unlocked file raises a Windows popup + a Desktop alert file + a non-zero exit.
"""

from __future__ import annotations

import argparse
import base64
import fcntl
import hashlib
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import verify as vfy  # noqa: E402

REPO = HERE.parents[1]
VAULT = Path(os.environ.get("DZ_VAULT_DIR", Path.home() / "ff-legacy-vault"))
OUTBOX = VAULT / "outbox"
STATUS_FILE = VAULT / "sync-status.json"
RECIPIENTS = HERE / "recipients.txt"
LOG_DIR = REPO / "data" / "logs"

DRIVE_REMOTE = "gdrive-vault:ff-legacy-vault"
ICLOUD_DIR = Path("/mnt/c/Users/MainUser/iCloudDrive/ff-legacy-vault")
GH_REPO = "ocmooch/ff-legacy-vault"

SUFFIX = ".tar.gz.age"
ARCHIVE_RE = re.compile(
    r"^dz-vault_(?P<kind>[a-z0-9-]+)_(?P<date>\d{8})_(?P<sha8>[0-9a-f]{8})\.tar\.gz\.age$"
)
PINNED_KIND = "seal-v1"
KEEP_NEWEST = 4

# Evergreen fallback copy (05 §10): read-only to this script, never synced or pruned.
EVERGREEN_FILE = "dz-vault_seal-v1_20261107_5ade3104.tar.gz.age"
EVERGREEN_SHA256 = "5ade31049ee588f701651753f8d71758bb054ad1f4a61c8bbab32bdea489d737"
EVERGREEN_SIZE = 22_481_806
EVERGREEN_SIDECARS = ("README.txt", f"{EVERGREEN_FILE}.sha256")
EVERGREEN_LOCAL = Path("/mnt/c/Users/MainUser/ff-legacy-evergreen")
EVERGREEN_ICLOUD = Path("/mnt/c/Users/MainUser/iCloudDrive/ff-legacy-evergreen")
EVERGREEN_DRIVE = "gdrive-vault:ff-legacy-evergreen"
ALERT_FILE = Path("/mnt/c/Users/MainUser/Desktop/VAULT-EVERGREEN-ALERT.txt")

log = logging.getLogger("vault_sync")


# ── naming + retention (pure) ────────────────────────────────────────────────


def archive_name(kind: str, day: str, sha256: str) -> str:
    return f"dz-vault_{kind}_{day}_{sha256[:8]}{SUFFIX}"


def plan_retention(names: list[str]) -> tuple[list[str], list[str]]:
    """Split archive names into (keep, delete). Non-archive names are ignored."""
    parsed = [(n, m) for n in names if (m := ARCHIVE_RE.match(n))]
    pinned = [n for n, m in parsed if m["kind"] == PINNED_KIND]
    others = sorted(((m["date"], n) for n, m in parsed if m["kind"] != PINNED_KIND), reverse=True)
    keep = sorted(pinned + [n for _, n in others[:KEEP_NEWEST]])
    delete = sorted(n for _, n in others[KEEP_NEWEST:])
    return keep, delete


# ── helpers ──────────────────────────────────────────────────────────────────


def run(*cmd: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if check and proc.returncode != 0:
        raise RuntimeError(
            f"{cmd[0]} {cmd[1] if len(cmd) > 1 else ''} failed: {proc.stderr.strip()[:500]}"
        )
    return proc


def file_hash(path: Path, algo: str) -> str:
    digest = hashlib.new(algo)
    with path.open("rb") as fh:
        for block in iter(lambda: fh.read(vfy.CHUNK), b""):
            digest.update(block)
    return digest.hexdigest()


def outbox_archives() -> list[Path]:
    return sorted(p for p in OUTBOX.glob(f"*{SUFFIX}") if ARCHIVE_RE.match(p.name))


def pack(src_dir: Path, kind: str) -> Path:
    """tar.gz + age-encrypt ``src_dir`` into the outbox; return the archive path."""
    OUTBOX.mkdir(parents=True, exist_ok=True)
    tmp = OUTBOX / f".packing-{kind}{SUFFIX}"
    tar = subprocess.Popen(
        [
            "tar",
            "-C",
            str(src_dir.parent),
            "--sort=name",
            "--owner=0",
            "--group=0",
            "--numeric-owner",
            "-czf",
            "-",
            src_dir.name,
        ],
        stdout=subprocess.PIPE,
    )
    age = subprocess.run(["age", "-R", str(RECIPIENTS), "-o", str(tmp)], stdin=tar.stdout)
    assert tar.stdout is not None
    tar.stdout.close()
    if tar.wait() != 0 or age.returncode != 0:
        tmp.unlink(missing_ok=True)
        raise RuntimeError("packing failed (tar or age)")
    sha = file_hash(tmp, "sha256")
    final = OUTBOX / archive_name(kind, datetime.now(tz=UTC).strftime("%Y%m%d"), sha)
    tmp.replace(final)
    log.info("packed %s (%.1f MB, sha256 %s)", final.name, final.stat().st_size / 1e6, sha)
    return final


# ── destinations ─────────────────────────────────────────────────────────────


@dataclass
class Destination:
    key: str

    def list(self) -> list[str]:
        raise NotImplementedError

    def verified(self, local: Path) -> bool:
        raise NotImplementedError

    def upload(self, local: Path) -> None:
        raise NotImplementedError

    def delete(self, name: str) -> None:
        raise NotImplementedError


class GoogleDrive(Destination):
    def __init__(self) -> None:
        super().__init__("gdrive")
        run("rclone", "mkdir", DRIVE_REMOTE)

    def list(self) -> list[str]:
        return run("rclone", "lsf", DRIVE_REMOTE, "--files-only").stdout.split()

    def verified(self, local: Path) -> bool:
        out = run("rclone", "md5sum", DRIVE_REMOTE, "--include", local.name).stdout.split()
        return bool(out) and out[0] == file_hash(local, "md5")

    def upload(self, local: Path) -> None:
        partial = f"{DRIVE_REMOTE}/.{local.name}.partial"
        run("rclone", "copyto", str(local), partial)
        run("rclone", "moveto", partial, f"{DRIVE_REMOTE}/{local.name}")

    def delete(self, name: str) -> None:
        run("rclone", "deletefile", f"{DRIVE_REMOTE}/{name}")


class ICloudFolder(Destination):
    def __init__(self) -> None:
        super().__init__("icloud")
        if not ICLOUD_DIR.is_dir():
            raise RuntimeError(
                f"iCloud folder missing: {ICLOUD_DIR} (is iCloud for Windows running?)"
            )

    def list(self) -> list[str]:
        return sorted(p.name for p in ICLOUD_DIR.iterdir() if p.is_file())

    def verified(self, local: Path) -> bool:
        remote = ICLOUD_DIR / local.name
        return remote.is_file() and file_hash(remote, "sha256") == file_hash(local, "sha256")

    def upload(self, local: Path) -> None:
        partial = ICLOUD_DIR / f".{local.name}.partial"
        shutil.copyfile(local, partial)
        partial.replace(ICLOUD_DIR / local.name)

    def delete(self, name: str) -> None:
        (ICLOUD_DIR / name).unlink()


class GitHubReleases(Destination):
    def __init__(self) -> None:
        super().__init__("github")
        vis = run("gh", "repo", "view", GH_REPO, "--json", "visibility", "--jq", ".visibility")
        if vis.stdout.strip() != "PRIVATE":
            raise RuntimeError(f"{GH_REPO} is not PRIVATE — refusing to upload")

    @staticmethod
    def tag(name: str) -> str:
        return name.removesuffix(SUFFIX)

    def list(self) -> list[str]:
        out = run(
            "gh", "api", f"repos/{GH_REPO}/releases", "--paginate", "--jq", ".[].assets[].name"
        ).stdout
        return out.split()

    def verified(self, local: Path) -> bool:
        proc = run(
            "gh", "api", f"repos/{GH_REPO}/releases/tags/{self.tag(local.name)}", check=False
        )
        if proc.returncode != 0:
            return False
        assets = [a for a in json.loads(proc.stdout).get("assets", []) if a["name"] == local.name]
        if not assets:
            return False
        want = file_hash(local, "sha256")
        digest = assets[0].get("digest")
        if digest:
            return digest == f"sha256:{want}"
        with tempfile.TemporaryDirectory() as tmp:  # older API: download and hash
            run(
                "gh",
                "release",
                "download",
                self.tag(local.name),
                "-R",
                GH_REPO,
                "-p",
                local.name,
                "-D",
                tmp,
            )
            return file_hash(Path(tmp) / local.name, "sha256") == want

    def upload(self, local: Path) -> None:
        tag = self.tag(local.name)
        exists = run("gh", "release", "view", tag, "-R", GH_REPO, check=False).returncode == 0
        if exists:
            run("gh", "release", "upload", tag, str(local), "-R", GH_REPO, "--clobber")
        else:
            notes = "Encrypted legacy-vault archive (age). See MANIFEST/verify.py in this repo."
            run(
                "gh",
                "release",
                "create",
                tag,
                str(local),
                "-R",
                GH_REPO,
                "--title",
                tag,
                "--notes",
                notes,
            )

    def delete(self, name: str) -> None:
        run("gh", "release", "delete", self.tag(name), "-R", GH_REPO, "--cleanup-tag", "--yes")


DESTINATIONS = (GoogleDrive, ICloudFolder, GitHubReleases)


# ── evergreen fallback check (read-only) ─────────────────────────────────────


def check_listing(names: dict[str, int]) -> list[str]:
    """Problems with a folder listing ``{name: size}`` (pure)."""
    expected = {EVERGREEN_FILE, *EVERGREEN_SIDECARS}
    problems = [f"MISSING {n}" for n in sorted(expected - names.keys())]
    problems += [f"UNEXPECTED file {n}" for n in sorted(names.keys() - expected)]
    size = names.get(EVERGREEN_FILE)
    if size is not None and size != EVERGREEN_SIZE:
        problems.append(f"SIZE CHANGED {EVERGREEN_FILE}: {size} != {EVERGREEN_SIZE}")
    return problems


def _windows_locked(path: Path) -> bool:
    """True if ``path`` still carries an NTFS deny-ACE (the lock we placed)."""
    win = run("wslpath", "-w", str(path)).stdout.strip()
    return "(DENY)" in run("icacls.exe", win).stdout


def check_folder(folder: Path, *, check_locks: bool = True) -> list[str]:
    if not folder.is_dir():
        return [f"FOLDER MISSING {folder}"]
    problems = check_listing({p.name: p.stat().st_size for p in folder.iterdir()})
    archive = folder / EVERGREEN_FILE
    if archive.is_file():
        if file_hash(archive, "sha256") != EVERGREEN_SHA256:
            problems.append(f"CONTENT CHANGED {EVERGREEN_FILE} (sha256 mismatch)")
        if archive.stat().st_mode & 0o222:
            problems.append(f"READ-ONLY attribute removed from {EVERGREEN_FILE}")
    if check_locks:
        for target in (folder, archive):
            if target.exists() and not _windows_locked(target):
                problems.append(f"LOCK REMOVED (no deny ACL) on {target.name}")
    return problems


def check_drive() -> list[str]:
    listing = json.loads(run("rclone", "lsjson", EVERGREEN_DRIVE).stdout)
    problems = check_listing({e["Name"]: e["Size"] for e in listing if not e["IsDir"]})
    if any(e["Name"] == EVERGREEN_FILE for e in listing):
        out = run(
            "rclone",
            "hashsum",
            "sha256",
            "--download",
            EVERGREEN_DRIVE,
            "--include",
            EVERGREEN_FILE,
        ).stdout.split()
        if not out or out[0] != EVERGREEN_SHA256:
            problems.append(f"CONTENT CHANGED {EVERGREEN_FILE} (sha256 mismatch)")
    return problems


def raise_alert(problems: dict[str, list[str]]) -> None:
    """Make the failure impossible to miss: Desktop file + modal Windows popup."""
    now = datetime.now(tz=UTC).strftime("%Y-%m-%d %H:%M UTC")
    lines = [
        "FF LEGACY VAULT - EVERGREEN FALLBACK COPY PROBLEM",
        f"Detected {now} by danger-zone scripts/vault/vault_sync.py check-evergreen",
        "",
    ]
    for where, items in problems.items():
        lines += [f"{where}:"] + [f"  - {p}" for p in items]
    lines += [
        "",
        "These copies must never change. If this was not deliberate, restore the",
        "file from one of the other evergreen copies (see their README.txt) and",
        "re-apply the lock - docs/sleeper/05_VAULT_OFFSITE_SYNC.md section 10.",
        "Re-check:  ~/danger-zone/scripts/vault/vault_sync.sh check-evergreen",
        "This file is removed automatically once the check passes again.",
    ]
    try:
        ALERT_FILE.write_text("\r\n".join(lines) + "\r\n")
        win = run("wslpath", "-w", str(ALERT_FILE)).stdout.strip()
        popup = (
            f"$m = Get-Content -Raw -LiteralPath '{win}'; "
            "(New-Object -ComObject WScript.Shell).Popup($m, 0, "
            "'FF VAULT ALERT: evergreen copy problem', 0x1030) | Out-Null"
        )
        enc = base64.b64encode(popup.encode("utf-16-le")).decode()
        # Start-Process detaches the popup so it outlives this job and WSL.
        run(
            "powershell.exe",
            "-NoProfile",
            "-Command",
            "Start-Process powershell.exe -WindowStyle Hidden "
            f"-ArgumentList '-NoProfile -EncodedCommand {enc}'",
        )
    except Exception as exc:  # the log + exit code still carry the alert
        log.error("could not raise the Windows alert: %s", exc)


def check_evergreen() -> int:
    checks = {
        "local": lambda: check_folder(EVERGREEN_LOCAL),
        "icloud": lambda: check_folder(EVERGREEN_ICLOUD),
        "gdrive": check_drive,
    }
    problems: dict[str, list[str]] = {}
    for where, fn in checks.items():
        try:
            found = fn()
        except Exception as exc:
            found = [f"COULD NOT CHECK: {str(exc)[:300]}"]
        if found:
            problems[where] = found
            for p in found:
                log.error("evergreen %-6s %s", where, p)
        else:
            log.info("evergreen %-6s ok", where)
    status = load_status()
    status["evergreen"] = {
        "last_check": datetime.now(tz=UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "ok": not problems,
        "problems": problems,
    }
    save_status(status)
    if problems:
        raise_alert(problems)
        return 1
    ALERT_FILE.unlink(missing_ok=True)
    return 0


# ── commands ─────────────────────────────────────────────────────────────────


def load_status() -> dict:
    try:
        return json.loads(STATUS_FILE.read_text())
    except (OSError, ValueError):
        return {"destinations": {}}


def save_status(status: dict) -> None:
    tmp = STATUS_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(status, indent=2, sort_keys=True) + "\n")
    tmp.replace(STATUS_FILE)


def sync() -> int:
    keep, _ = plan_retention([p.name for p in outbox_archives()])
    archives = [OUTBOX / n for n in keep]
    if not archives:
        log.warning("outbox is empty — nothing to sync")
        return 0
    status = load_status()
    failures = 0
    for cls in DESTINATIONS:
        key = cls.__name__
        entry = {}
        try:
            dest = cls()
            key = dest.key
            entry = status["destinations"].setdefault(key, {})
            for local in archives:
                if dest.verified(local):
                    log.info("%-7s already has %s", key, local.name)
                    continue
                log.info("%-7s uploading %s", key, local.name)
                dest.upload(local)
                if not dest.verified(local):
                    raise RuntimeError(f"hash mismatch after upload of {local.name}")
                log.info("%-7s verified %s", key, local.name)
            _, delete = plan_retention(dest.list())
            for name in delete:
                log.info("%-7s retention: deleting %s", key, name)
                dest.delete(name)
            newest = max(archives, key=lambda p: (p.name.split("_")[2], p.name))
            entry.update(
                last_success=datetime.now(tz=UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
                file=newest.name,
                sha256=file_hash(newest, "sha256"),
                last_error=None,
            )
        except Exception as exc:  # one destination failing must not stop the others
            failures += 1
            log.error("%-7s FAILED: %s", key, exc)
            status["destinations"].setdefault(key, entry)["last_error"] = str(exc)[:500]
        save_status(status)
    if not failures:
        _, delete = plan_retention([p.name for p in outbox_archives()])
        for name in delete:
            log.info("outbox  retention: deleting %s", name)
            (OUTBOX / name).unlink()
    return 1 if failures else 0


def snapshot() -> Path:
    import build_vault_dir as builder

    work = VAULT / "work"
    work.mkdir(parents=True, exist_ok=True)
    label = f"snap-{datetime.now(tz=UTC).strftime('%Y%m%dT%H%M%SZ')}"
    out = work / label
    try:
        builder.build(
            out, label, "snapshot", extra_dirs={"raw/sleeper": REPO / "data" / "raw" / "sleeper"}
        )
        if vfy.verify(out) != 0:
            raise RuntimeError("snapshot failed its own verify.py")
        return pack(out, "snap")
    finally:
        shutil.rmtree(out, ignore_errors=True)


def setup_logging() -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
    handlers: list[logging.Handler] = [
        logging.FileHandler(LOG_DIR / f"vault_sync-{datetime.now(tz=UTC):%Y-%m-%d}.log"),
        logging.StreamHandler(),
    ]
    for h in handlers:
        h.setFormatter(fmt)
        log.addHandler(h)
    log.setLevel(logging.INFO)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="cmd")
    p_seal = sub.add_parser("pack-seal")
    p_seal.add_argument("seal_dir", type=Path)
    sub.add_parser("snapshot")
    sub.add_parser("sync")
    sub.add_parser("check-evergreen")
    sub.add_parser("status")
    args = parser.parse_args()

    if args.cmd == "status":
        print(json.dumps(load_status(), indent=2))
        return 0
    VAULT.mkdir(parents=True, exist_ok=True)
    setup_logging()
    with (VAULT / ".sync.lock").open("w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            log.error("another vault_sync is running")
            return 1
        try:
            if args.cmd == "pack-seal":
                seal = args.seal_dir.resolve()
                if vfy.verify(seal) != 0:
                    return 1
                kind = json.loads((seal / vfy.MANIFEST_NAME).read_text())["label"]
                pack(seal, kind)
                return 0
            if args.cmd == "snapshot":
                snapshot()
                return 0
            if args.cmd == "check-evergreen":
                return check_evergreen()
            if args.cmd == "sync":
                return sync()
        except Exception as exc:
            log.error("FAILED: %s", exc)
            return 1
        # weekly job: the evergreen check runs even if the snapshot or sync failed
        rc = 1
        try:
            snapshot()
            rc = sync()
        except Exception as exc:
            log.error("FAILED: %s", exc)
        return max(rc, check_evergreen())


if __name__ == "__main__":
    sys.exit(main())
