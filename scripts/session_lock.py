"""
scripts/session_lock.py
========================
Trivial cross-session commit lock for the Sensitivity & Config Migration
Campaign (docs/BACKLOG.md, "Sensitivity & Config Migration Campaign"
section). A single text file at the repo root, `.session-lock` -- no new
dependencies, no git hook installed. This is a manual discipline aid, not
an enforced git hook: run it yourself before/after each commit made as
part of this campaign.

Usage
-----
    python scripts/session_lock.py acquire --session sensitivity-config-migration
    ... do the commit ...
    python scripts/session_lock.py release --session sensitivity-config-migration

`acquire` behaviour
--------------------
  - No lock file present             -> create it, exit 0.
  - Lock file present, same session  -> already ours, proceed, exit 0.
  - Lock file present, different
    session, modified < 4h ago       -> BLOCKED: print an error, exit 2.
                                         Caller must not proceed with the
                                         commit -- another session may be
                                         mid-work.
  - Lock file present, different
    session, modified >= 4h ago      -> stale; reclaim it for this
                                         session, exit 0 (prints a
                                         warning).

`release` only removes the lock file if it currently belongs to the
session name passed in -- refuses to remove a lock it does not own.
"""
from __future__ import annotations

import argparse
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
LOCK_PATH = REPO_ROOT / ".session-lock"
STALE_AFTER_SECONDS = 4 * 60 * 60  # 4 hours


def _read_lock() -> dict:
    data: dict = {}
    for line in LOCK_PATH.read_text(encoding="utf-8").splitlines():
        if "=" in line:
            k, _, v = line.partition("=")
            data[k.strip()] = v.strip()
    return data


def _write_lock(session: str) -> None:
    started = datetime.now(timezone.utc).isoformat()
    LOCK_PATH.write_text(f"session={session}\nstarted={started}\n", encoding="utf-8")


def acquire(session: str) -> int:
    if not LOCK_PATH.exists():
        _write_lock(session)
        print(f"[session-lock] created for '{session}'.")
        return 0

    data = _read_lock()
    owner = data.get("session", "")
    age_seconds = time.time() - LOCK_PATH.stat().st_mtime

    if owner == session:
        print(f"[session-lock] already held by this session ('{session}') -- proceeding.")
        return 0

    if age_seconds < STALE_AFTER_SECONDS:
        print(
            f"[session-lock] BLOCKED: lock held by session '{owner}' "
            f"({age_seconds / 60:.0f} min ago, started={data.get('started', '?')}). "
            "Do not commit -- another session may be mid-work. Stopping."
        )
        return 2

    print(
        f"[session-lock] stale lock from session '{owner}' "
        f"({age_seconds / 3600:.1f}h old) -- reclaiming for '{session}'."
    )
    _write_lock(session)
    return 0


def release(session: str) -> int:
    if not LOCK_PATH.exists():
        print("[session-lock] no lock file present -- nothing to release.")
        return 0

    data = _read_lock()
    owner = data.get("session", "")
    if owner != session:
        print(
            f"[session-lock] refusing to release: lock is owned by "
            f"'{owner}', not '{session}'."
        )
        return 1

    LOCK_PATH.unlink()
    print(f"[session-lock] released ('{session}').")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["acquire", "release"])
    parser.add_argument("--session", required=True)
    args = parser.parse_args()

    if args.action == "acquire":
        return acquire(args.session)
    return release(args.session)


if __name__ == "__main__":
    sys.exit(main())
