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
        # Skip debrid placeholder files (32-hex-char names like
        # 1c39bf4d....mkv): Sonarr hasn't renamed/imported them yet and Plex
        # can't match them (no SxxExx to parse), so rescans never fix them.
        "skip_hash_names": os.getenv("SKIP_HASH_NAMES", "true").lower() == "true",
        # Max dirs triggered per run (0 = unlimited). Paces the backlog so
        # Plex/autoscan can absorb it - e.g. 200/run at 5s scan-delay ≈ 17min
        # of scan queue instead of an 8h thundering herd. Remainder next run.
        "max_triggers": int(os.getenv("MAX_TRIGGERS", "200")),
        # Mount sanity guard: abort the run if more than this % of Plex paths
        # are missing (a remounted/dead FUSE mount makes EVERYTHING look stale -
        # readdir still lists names while all symlink targets dangle).
        # 100 = disable. No triggers fired, no state written on abort.
        "max_stale_pct": float(os.getenv("MAX_STALE_PCT", "25")),
        # Optional Sonarr: resolve hash-named files to episodes and rename them
        # (Plex can't match hash names, so autoscan rescans never fix those).
        "sonarr_url": os.getenv("SONARR_URL", ""),
        "sonarr_api_key": os.getenv("SONARR_API_KEY", ""),
        # Fire Sonarr RenameFiles for hash files (false = only log the preview).
        "sonarr_auto_rename": os.getenv("SONARR_AUTO_RENAME", "false").lower() == "true",
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


def _ep_tag(path: str) -> str | None:
    """Extract normalized SxxExx tag from a filename, or None."""
    m = re.search(r"[Ss](\d{1,2})[Ee](\d{1,3})", os.path.basename(path))
    return f"S{int(m.group(1)):02d}E{int(m.group(2)):02d}" if m else None


def _find_renames(stale_by_dir: dict[str, list[str]], unknown_by_dir: dict[str, list[str]]) -> list[tuple[str, str, str, str]]:
    """Match stale vs unknown files as probable renames. Returns (dir, old, new, basis).

    A dir flagged on BOTH sides means something changed there. Pairs are formed by:
    - TV: same episode tag (SxxExx), different basename (e.g. spaces-vs-dots rename)
    - Fallback: exactly one stale + one unknown file with the same container
      (typical *arr upgrade swapping the file, movies included)
    """
    pairs: list[tuple[str, str, str, str]] = []
    ext = lambda p: p.rsplit(".", 1)[-1].lower() if "." in p else ""
    for d in sorted(set(stale_by_dir) & set(unknown_by_dir)):
        old_files = stale_by_dir[d]
        new_files = unknown_by_dir[d]
        # Strict 1-vs-1 dirs: something was swapped here (rename or upgrade).
        if len(old_files) == 1 and len(new_files) == 1:
            o, n = old_files[0], new_files[0]
            if os.path.basename(o) != os.path.basename(n):
                t_old, t_new = _ep_tag(o), _ep_tag(n)
                if t_old and t_old == t_new:
                    pairs.append((d, o, n, f"same episode {t_old}"))
                elif ext(o) == ext(n):
                    pairs.append((d, o, n, "single swap, same container"))
            continue
        # Busy dirs: only pair by matching episode tag (never pair leftovers -
        # a deleted file plus an unrelated new file are not a rename).
        old_by_tag: dict[str, list[str]] = {}
        for p in old_files:
            t = _ep_tag(p)
            if t:
                old_by_tag.setdefault(t, []).append(p)
        for p in new_files:
            t = _ep_tag(p)
            if t and t in old_by_tag:
                for old in old_by_tag[t]:
                    if os.path.basename(old) != os.path.basename(p):
                        pairs.append((d, old, p, f"same episode {t}"))
    return pairs


def under_roots(path: str, roots: list[str]) -> bool:
    """True if path lives under one of the scan roots."""
    for r in roots:
        if path == r or path.startswith(r + os.sep):
            return True
    return False


HASH_NAME = re.compile(r"^[a-f0-9]{32}\.")


def walk_media_files(roots: list[str], exts: set[str], skip_hash_names: bool = True) -> tuple[set[str], list[str]]:
    """Collect media files under roots (follows dir symlinks, never deletes anything).

    Returns (media_files, hash_files) where hash_files are debrid placeholders
    (32-hex names) collected separately for Sonarr-side resolution.
    """
    found: set[str] = set()
    hash_files: list[str] = []
    for root in roots:
        if not os.path.isdir(root):
            log.warning(f"Scan root not a directory, skipping: {root}")
            continue
        for dirpath, _dirnames, filenames in os.walk(root, followlinks=True):
            for name in filenames:
                suffix = name.rsplit(".", 1)[-1].lower() if "." in name else ""
                if suffix not in exts:
                    continue
                full = norm(os.path.join(dirpath, name))
                if HASH_NAME.match(name):
                    hash_files.append(full)
                    if skip_hash_names:
                        continue
                found.add(full)
                if len(found) % 10000 == 0:
                    log.info(f"... still walking disk: {len(found)} files so far (in {dirpath})")
    if skip_hash_names and hash_files:
        log.info(f"Holding {len(hash_files)} debrid placeholder files for Sonarr-side resolution (not Plex-compared)")
    return found, hash_files


SEASON_DIR = re.compile(r"[Ss]eason\s*(\d+)\s*$")


def _process_hash_files(config: dict, sonarr, hash_files: list[str], triggered: dict[str, float], now: float) -> None:
    """Resolve debrid placeholder files via Sonarr and optionally rename them.

    Hash-named files (no SxxExx to parse) can never be matched by Plex, so
    autoscan rescans can't fix them - but Sonarr already links each file to an
    episode. Groups them by (series, season), previews via GET /api/v3/rename,
    and fires RenameFiles when SONARR_AUTO_RENAME=true. Cooldown keys look like
    'sonarr:{seriesId}:{season}' and never enter the autoscan batch.
    """
    if not hash_files:
        return
    naming = sonarr.get_naming()
    if naming and not naming.get("renameEpisodes", True):
        log.info(
            "Sonarr episode renaming is OFF - rename preview would always be empty, "
            "skipping it (hash files still flow to autoscan when SKIP_HASH_NAMES=false)"
        )
        if config["sonarr_auto_rename"]:
            log.warning("SONARR_AUTO_RENAME=true but Sonarr renaming is OFF - rename commands will no-op")
        return
    try:
        series = sonarr.get_series()
    except Exception as e:
        log.error(f"Sonarr series list failed: {e}")
        return
    by_path: dict[str, tuple[int, str]] = {}
    for s in series:
        try:
            by_path[norm(s["path"])] = (int(s["id"]), s.get("title", "?"))
        except (KeyError, TypeError, ValueError):
            continue
    groups: dict[tuple[int, int], dict] = {}
    unmapped: set[str] = set()
    for f in hash_files:
        season_dir = os.path.dirname(f)
        show_dir = os.path.dirname(season_dir)
        m = SEASON_DIR.search(os.path.basename(season_dir))
        hit = by_path.get(show_dir)
        if not hit or not m:
            if hit is None:
                unmapped.add(show_dir)
            continue
        key = (hit[0], int(m.group(1)))
        g = groups.setdefault(key, {"title": hit[1], "files": []})
        g["files"].append(f)
    for show_dir in sorted(unmapped):
        log.warning(f"Hash files under unmapped show dir (no Sonarr series path match): {show_dir}")
    cooldown_s = config["cooldown_hours"] * 3600
    for (series_id, season), g in sorted(groups.items()):
        state_key = f"sonarr:{series_id}:{season}"
        if now - triggered.get(state_key, 0) < cooldown_s:
            log.info(f"Skipping Sonarr rename for {g['title']} season {season} (cooldown)")
            continue
        try:
            preview = sonarr.get_renames(series_id, season)
        except Exception as e:
            log.error(f"Sonarr rename preview failed for {g['title']} S{season}: {e}")
            continue
        mine = {norm(os.path.basename(p)) for p in g["files"]}
        hits = [r for r in preview
                if isinstance(r, dict) and norm(os.path.basename(r.get("existingPath", ""))) in mine]
        if not hits:
            log.info(
                f"Sonarr reports nothing to rename for {g['title']} season {season} "
                f"({len(g['files'])} hash files - may need manual import in Sonarr first)"
            )
            if not config["dry_run"]:
                triggered[state_key] = now
            continue
        for r in hits:
            log.info(f"Sonarr rename [{g['title']} S{season}]: '{os.path.basename(r['existingPath'])}' -> '{os.path.basename(r['newPath'])}'")
        if config["dry_run"]:
            log.info(f"[DRY RUN] Would fire Sonarr RenameFiles for {len(hits)} files ({g['title']} S{season})")
            continue
        if not config["sonarr_auto_rename"]:
            log.info(f"Set SONARR_AUTO_RENAME=true to apply ({g['title']} S{season})")
            continue
        ids = [r["episodeFileId"] for r in hits if r.get("episodeFileId") is not None]
        if ids and sonarr.rename_files(series_id, ids):
            log.info(f"Sonarr RenameFiles queued for {len(ids)} files ({g['title']} S{season}) - rescan will pick them up")
            triggered[state_key] = now
        else:
            log.error(f"Sonarr RenameFiles failed for {g['title']} S{season}")


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

    # --- Disk side (readdir walk - fast unless the FUSE mount is unhealthy) ---
    log.info("Walking disk under scan roots...")
    t0 = time.time()
    disk_files, hash_files = walk_media_files(roots, config["media_exts"], config["skip_hash_names"])
    log.info(f"Found {len(disk_files)} media files on disk under scan roots ({time.time() - t0:.0f}s)")

    # --- Sonarr side: resolve hash-named placeholders (Plex can never match these) ---
    now = time.time()
    triggered = load_state(config["state_file"])
    if config["sonarr_url"] and config["sonarr_api_key"]:
        from .clients import SonarrClient

        try:
            _process_hash_files(config, SonarrClient(config["sonarr_url"], config["sonarr_api_key"]), hash_files, triggered, now)
        except Exception as e:
            log.error(f"Sonarr hash-file pass failed: {e}")
    elif config["sonarr_url"]:
        log.warning("SONARR_URL set without SONARR_API_KEY - skipping Sonarr hash-file resolution")

    # --- Diff (both directions collapse to directory scans) ---
    # NOTE: os.path.exists() stats every Plex path - on a healthy mount this
    # takes ~1min for 50k files. If logs stall here, the FUSE mount is sick
    # (e.g. host remount without restarting this container -> stale view).
    log.info(f"Checking {len(plex_files)} Plex paths against disk...")
    t0 = time.time()
    stale_by_dir: dict[str, list[str]] = {}
    for p in plex_files:
        if not os.path.exists(p):
            stale_by_dir.setdefault(os.path.dirname(p), []).append(p)
    log.info(f"Path check finished in {time.time() - t0:.0f}s")
    unknown_by_dir: dict[str, list[str]] = {}
    for p in disk_files - plex_files:
        unknown_by_dir.setdefault(os.path.dirname(p), []).append(p)
    stale = sorted(stale_by_dir)
    unknown = sorted(unknown_by_dir)
    stale_set = set(stale)
    # Stale dirs first (broken Plex entries beat missing ones), then unknown-only.
    wanted = stale + [d for d in unknown if d not in stale_set]
    n_stale_files = sum(len(v) for v in stale_by_dir.values())
    n_unknown_files = sum(len(v) for v in unknown_by_dir.values())
    if not plex_files:
        log.error("No Plex paths collected - aborting run (Plex unreachable or all sections empty?)")
        return
    stale_pct = 100.0 * n_stale_files / len(plex_files)
    max_stale_pct = config["max_stale_pct"]
    if stale_pct > max_stale_pct:
        log.error(
            f"ABORTING run: {stale_pct:.1f}% of Plex paths missing ({n_stale_files}/{len(plex_files)}), "
            f"over MAX_STALE_PCT={max_stale_pct:g}. The mount is almost certainly dead "
            "(e.g. FUSE remount with dangling symlinks) - not real drift. No triggers fired, state untouched."
        )
        return
    renames = _find_renames(stale_by_dir, unknown_by_dir)
    for d, old, new, basis in renames:
        log.info(f"Probable rename ({basis}): '{os.path.basename(old)}' -> '{os.path.basename(new)}' in {d}")
    log.info(
        f"Diff: {len(plex_files)} Plex paths, {len(disk_files)} disk files, "
        f"{n_stale_files} stale files in {len(stale)} dirs, "
        f"{n_unknown_files} unknown files in {len(unknown)} dirs, "
        f"{len(renames)} probable renames"
    )
    for d in stale:
        log.info(f"Stale Plex path, needs rescan: {d} ({len(stale_by_dir[d])} dead, e.g. {stale_by_dir[d][0]})")
    for d in unknown:
        log.info(f"On disk but unknown to Plex, needs rescan: {d} ({len(unknown_by_dir[d])} new, e.g. {unknown_by_dir[d][0]})")

    if not wanted:
        log.info("Complete: everything in sync, nothing to trigger")
        return

    # --- Cooldown filter (triggered{} already holds Sonarr keys from above) ---
    cooldown_s = config["cooldown_hours"] * 3600
    fresh = [d for d in wanted if now - triggered.get(d, 0) >= cooldown_s]
    skipped = len(wanted) - len(fresh)
    if skipped:
        log.info(f"Skipping {skipped} dirs still in cooldown window ({config['cooldown_hours']}h)")

    if not fresh:
        log.info("Complete: all mismatches in cooldown, nothing to trigger")
        return

    max_triggers = max(0, config["max_triggers"])
    if max_triggers and len(fresh) > max_triggers:
        log.info(f"Capping this run at {max_triggers} of {len(fresh)} dirs (stale first); remainder next run")
        fresh = fresh[:max_triggers]

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
    if not config["dry_run"]:
        # Persists autoscan marks AND any Sonarr cooldown keys set above.
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
