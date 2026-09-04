"""Shared launcher for the browser-backed scrapers (Lufthansa, China Airlines).

One thing matters here: the Chrome profile has to *persist across process
restarts*. Both sites bind their bot-protection clearance to the browser
that earned it - Cloudflare's `cf_clearance`, Akamai's `bm_sz`/`_abck`,
Imperva's `reese84`, DataDome's cookie - and a brand-new, cookie-less profile
on every start is exactly the fingerprint those systems score hardest. A
profile that already carries yesterday's cookies walks in like a returning
visitor: no challenge to solve on the first search, and a far lower chance
of the "processing your request" interstitial or an outright deny.

The profile lives under ~/.cache/fare-scraper/<name>-profile. Chrome locks a
profile directory while it is open, so if a second process (a CLI run while
the server is up, say) cannot take it, that process falls back to a throwaway
directory rather than failing - it just pays the first-search cost.
"""

from __future__ import annotations

import os
import sys
import uuid
from pathlib import Path

CACHE = Path.home() / ".cache" / "fare-scraper"
LOCKS = ("SingletonLock", "SingletonSocket", "SingletonCookie")


def _clear_stale_lock(profile: Path) -> bool:
    """Chrome leaves `SingletonLock -> <host>-<pid>` behind when it dies
    without cleaning up (a killed process, a crash). If that pid is gone the
    lock is stale, and honouring it would push every later run onto a
    throwaway profile for no reason - quietly defeating profile persistence.
    Returns True if a stale lock was removed."""
    lock = profile / "SingletonLock"
    if not lock.is_symlink():
        return False
    target = os.readlink(lock)
    try:
        pid = int(target.rsplit("-", 1)[1])
        os.kill(pid, 0)          # raises if no such process
        return False             # alive: genuinely busy
    except (ValueError, IndexError, ProcessLookupError):
        pass
    except PermissionError:
        return False             # exists, owned by someone else: busy
    for f in LOCKS:
        try:
            (profile / f).unlink()
        except FileNotFoundError:
            pass
    return True


def launch_persistent(pw, name: str, headless: bool = False, persistent: bool = True):
    """A patchright context for `name`.

    `persistent=True` reuses ~/.cache/fare-scraper/<name>-profile across
    restarts - right for Lufthansa, whose Cloudflare clearance is worth
    keeping. `persistent=False` starts from a throwaway profile every time -
    right for China Airlines, where DataDome/Imperva reputation *sticks* to
    a profile: one that has tripped des-portal's behavioural gate keeps
    failing (0 of 3 runs, ~200 s each) while a fresh one passes (3 of 3).
    """
    opts = dict(channel="chrome", headless=headless,
                viewport={"width": 1280, "height": 900})
    if not persistent:
        tmp = Path(f"/tmp/{name}_patchright_{uuid.uuid4().hex[:8]}")
        return pw.chromium.launch_persistent_context(user_data_dir=str(tmp), **opts)
    profile = CACHE / f"{name}-profile"
    profile.mkdir(parents=True, exist_ok=True)
    for attempt in (1, 2):
        try:
            return pw.chromium.launch_persistent_context(user_data_dir=str(profile), **opts)
        except Exception as e:  # noqa: BLE001 - a locked profile must not stop a search
            if attempt == 1 and _clear_stale_lock(profile):
                continue         # dead owner: lock removed, try the real profile once more
            print(f"warn: {name} profile busy ({str(e)[:60]}); using a throwaway one",
                  file=sys.stderr)
            tmp = Path(f"/tmp/{name}_patchright_{uuid.uuid4().hex[:8]}")
            return pw.chromium.launch_persistent_context(user_data_dir=str(tmp), **opts)
