"""Plex path auditor - 2-way diff of Plex DB paths vs disk, via Autoscan."""

import json
import logging
import os
import re
import sys
import time
from pathlib import Path

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s"
)
log = logging.getLogger(__name__)


def parse_interval(value: str) -> int | None:
    """Parse interval string like '6h', '30m', '1d' to seconds. Returns None if not set."""
    if not value:
        return None
    match = re.match(r"^(\d+)([smhd])$", value.lower())
    if not match:
        log.warning(f"Invalid RUN_INTERVAL format: {value}. Use format like '6h', '30m', '1d'")
        return None
    num, unit = int(match.group(1)), match.group(2)
    multipliers = {"s": 1, "m": 60, "h": 3600, "d": 86400}
    return num * multipliers[unit]


def _parse_list(value: str) -> list[str]:
    """Parse comma-separated list, skipping blanks."""
    return [p.strip() for p in (value or "").split(",") if p.strip()]


def load_config() -> dict:
    """Load configuration from environment variables."""
    required = ["PLEX_URL", "PLEX_TOKEN", "AUTOSCAN_URL", "AUTOSCAN_USER", "AUTOSCAN_PASS"]
    missing = [k for k in required if not os.getenv(k)]
    if missing:
        log.error(f"Missing required env vars: {', '.join(missing)}")
        sys.exit(1)

    return {
        "plex_url": os.environ["PLEX_URL"],
        "plex_token": os.environ["PLEX_TOKEN"],
        "autoscan_url": os.environ["AUTOSCAN_URL"],
        "autoscan_user": os.environ["AUTOSCAN_USER"],
        "autoscan_pass": os.environ["AUTOSCAN_PASS"],
        # Library roots walked on disk AND used to scope Plex paths.
        "scan_roots": _parse_list(os.getenv("SCAN_ROOTS", "/mnt/plex/TV,/mnt/plex/Movies")),
        "media_exts": {e.lower().lstrip(".") for e in _parse_list(os.getenv("MEDIA_EXTS", "mkv,mp4,avi,m4v,ts"))},
        "dry_run": os.getenv("DRY_RUN", "false").lower() == "true",
        "state_file": Path(os.getenv("STATE_FILE", "/config/triggered.json")),
        # Don't re-trigger the same dir within this many hours.
        "cooldown_hours": float(os.getenv("COOLDOWN_HOURS", "24")),
        "run_interval": parse_interval(os.getenv("RUN_INTERVAL", "")),
        "page_size": int(os.getenv("PAGE_SIZE", "500")),
        # Max dirs per autoscan request (URL length safety).
        "batch_size": int(os.getenv("BATCH_SIZE", "50")),
    }


def load_state(path: Path) -> dict[str, float]:
    """Load dir -> epoch-seconds map of triggered scans."""
    if path.exists():
        try:
            data = json.loads(path.read_text())
            return {str(k): float(v) for k, v in data.items()}
        except (json.JSONDecodeError, TypeError, ValueError):
            return {}
    return {}


def save_state(path: Path, state: dict[str, float]) -> None:
    """Save triggered-dir timestamps."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state))


def norm(p: str) -> str:
    """Normalize a path for comparison (no symlink resolving - Plex stores the link path)."""
    return os.path.normpath(p)


def under_roots(path: str, roots: list[str]) -> bool:
    """True if path lives under one of the scan roots."""
    for r in roots:
        if path == r or path.startswith(r + os.sep):
            return True
    return False


def walk_media_files(roots: list[str], exts: set[str]) -> set[str]:
    """Collect media files under roots (follows dir symlinks, never deletes anything)."""
    found: set[str] = set()
    for root in roots:
        if not os.path.isdir(root):
            log.warning(f"Scan root not a directory, skipping: {root}")
            continue
        for dirpath, _dirnames, filenames in os.walk(root, followlinks=True):
            for name in filenames:
                suffix = name.rsplit(".", 1)[-1].lower() if "." in name else ""
                if suffix in exts:
                    found.add(norm(os.path.join(dirpath, name)))
    return found


def run_once(config: dict) -> None:
    """Run one audit cycle."""
    from .clients import AutoscanClient, PlexClient, EPISODE, MOVIE

    log.info("Starting Plex path audit")
    plex = PlexClient(config["plex_url"], config["plex_token"], config["page_size"])
    autoscan = AutoscanClient(config["autoscan_url"], config["autoscan_user"], config["autoscan_pass"])
    roots = [norm(r) for r in config["scan_roots"]]

    # --- Plex side: every Part.file in show+movie sections, scoped to roots ---
    try:
        sections = plex.get_sections()
    except Exception as e:
        log.error(f"Failed to list Plex sections: {e}")
        sys.exit(1)

    plex_files: set[str] = set()
    out_of_scope = 0
    for s in sections:
        if s["type"] not in ("show", "movie"):
            continue
        media_type = EPISODE if s["type"] == "show" else MOVIE
        try:
            files = plex.list_section_files(s["key"], media_type)
        except Exception as e:
            log.error(f"Failed to list section {s['title']}: {e}")
            sys.exit(1)
        log.info(f"Section '{s['title']}': {len(files)} files in Plex DB")
        for f in files:
            f = norm(f)
            if under_roots(f, roots):
                plex_files.add(f)
            else:
                out_of_scope += 1
    if out_of_scope:
        log.info(f"Ignored {out_of_scope} Plex paths outside SCAN_ROOTS")

    # --- Disk side ---
    disk_files = walk_media_files(roots, config["media_exts"])
    log.info(f"Found {len(disk_files)} media files on disk under scan roots")

    # --- Diff (both directions collapse to directory scans) ---
    stale_by_dir: dict[str, list[str]] = {}
    for p in plex_files:
        if not os.path.exists(p):
            stale_by_dir.setdefault(os.path.dirname(p), []).append(p)
    unknown_by_dir: dict[str, list[str]] = {}
    for p in disk_files - plex_files:
        unknown_by_dir.setdefault(os.path.dirname(p), []).append(p)
    stale = sorted(stale_by_dir)
    unknown = sorted(unknown_by_dir)
    wanted = sorted(set(stale) | set(unknown))
    n_stale_files = sum(len(v) for v in stale_by_dir.values())
    n_unknown_files = sum(len(v) for v in unknown_by_dir.values())
    log.info(
        f"Diff: {len(plex_files)} Plex paths, {len(disk_files)} disk files, "
        f"{n_stale_files} stale files in {len(stale)} dirs, "
        f"{n_unknown_files} unknown files in {len(unknown)} dirs"
    )
    for d in stale:
        log.info(f"Stale Plex path, needs rescan: {d} ({len(stale_by_dir[d])} dead, e.g. {stale_by_dir[d][0]})")
    for d in unknown:
        log.info(f"On disk but unknown to Plex, needs rescan: {d} ({len(unknown_by_dir[d])} new, e.g. {unknown_by_dir[d][0]})")

    if not wanted:
        log.info("Complete: everything in sync, nothing to trigger")
        return

    # --- Cooldown filter ---
    now = time.time()
    cooldown_s = config["cooldown_hours"] * 3600
    triggered = load_state(config["state_file"])
    fresh = [d for d in wanted if now - triggered.get(d, 0) >= cooldown_s]
    skipped = len(wanted) - len(fresh)
    if skipped:
        log.info(f"Skipping {skipped} dirs still in cooldown window ({config['cooldown_hours']}h)")

    if not fresh:
        log.info("Complete: all mismatches in cooldown, nothing to trigger")
        return

    if config["dry_run"]:
        for d in fresh:
            log.info(f"[DRY RUN] Would trigger autoscan for: {d}")
        log.info(f"Complete (dry run): {len(fresh)} dirs would be triggered")
        return

    # --- Trigger in batches; only mark dirs from successful requests ---
    batch_size = max(1, config["batch_size"])
    ok_dirs: list[str] = []
    for i in range(0, len(fresh), batch_size):
        batch = fresh[i:i + batch_size]
        if autoscan.trigger_dirs(batch):
            log.info(f"Triggered autoscan for {len(batch)} dirs")
            ok_dirs.extend(batch)
        else:
            log.error(f"Autoscan request failed for batch starting at {batch[0]} - will retry next run")

    for d in ok_dirs:
        triggered[d] = now
    if ok_dirs and not config["dry_run"]:
        save_state(config["state_file"], triggered)
    log.info(f"Complete: triggered {len(ok_dirs)}/{len(fresh)} dirs ({skipped} in cooldown)")


def main():
    """Main entry point with optional scheduler."""
    config = load_config()
    interval = config["run_interval"]

    if interval:
        log.info(f"Scheduler enabled: running every {interval}s")
        while True:
            try:
                run_once(config)
            except Exception as e:
                log.error(f"Run failed: {e}")
            log.info(f"Sleeping for {interval}s...")
            time.sleep(interval)
    else:
        run_once(config)


if __name__ == "__main__":
    main()
