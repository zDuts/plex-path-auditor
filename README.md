# Plex Path Auditor

2-way diff of Plex library paths vs disk, repaired via Autoscan. Catches exactly the class of bug where Plex points at a renamed file (e.g. spaces vs dots) or a file exists on disk that Plex never saw.

> Built for `linux/amd64` + `linux/arm64`.

## How it works

1. Lists every show + movie section via Plex (`/library/sections`), pages all episodes/movies, collects every `Part.file` path
2. Walks `SCAN_ROOTS` on disk for media files (read-only mount, never deletes anything)
3. Diffs both ways — everything collapses to **directory** scans (Autoscan's manual trigger takes dirs, not files):
   - Plex path missing on disk → stale entry → scan its directory (picks up the renamed file, like the S03E02 spaces-vs-dots case)
   - Disk file unknown to Plex → scan its directory
4. Dedupes dirs, skips dirs triggered within `COOLDOWN_HOURS`, fires batched `POST /triggers/manual?dir=...` requests

Only what you watch is irrelevant here — this audits the whole library by design. Scope it with `SCAN_ROOTS`.

## Requirements

- Autoscan with a working `manual` trigger. Verify first:
  ```bash
  curl -X POST 'http://your-autoscan:3030/triggers/manual?dir=%2Fmnt%2Fplex%2FTV' -u your_autoscan_user
  ```
- Autoscan `anchors` must resolve — if the anchor file is missing, Autoscan silently sends nothing and this tool's triggers go nowhere
- The auditor needs the **same `/mnt` view as Plex**, mounted with **`rslave` propagation** (see compose). With Docker's default `rprivate`, any host remount (rclone/nzbdav re-pull) leaves the container staring at a stale, pre-remount filesystem — every Plex path then looks missing and the run either stalls on dead stats or fires thousands of bogus triggers (the `MAX_STALE_PCT` guard aborts that case). Apply `rslave` to **every** container bind-mounting `/mnt` (Plex included), and recreate containers after host remounts — plain restarts keep old mounts

## Installation

```bash
docker pull ghcr.io/zduts/plex-path-auditor:latest
```

Sample compose — see `docker-compose.example.yml`. Start with `DRY_RUN=true`, check logs, then flip it.

## Environment variables

| Var | Required | Default | Description |
|-----|----------|---------|-------------|
| `PLEX_URL` | yes | — | Plex server URL |
| `PLEX_TOKEN` | yes | — | Plex X-Token |
| `AUTOSCAN_URL` | yes | — | Autoscan base URL (e.g. `http://autoscan:3030`) |
| `AUTOSCAN_USER` | yes | — | Autoscan basic-auth username |
| `AUTOSCAN_PASS` | yes | — | Autoscan basic-auth password |
| `SCAN_ROOTS` | no | `/mnt/plex/TV,/mnt/plex/Movies` | Roots walked on disk; Plex paths outside them are ignored |
| `MEDIA_EXTS` | no | `mkv,mp4,avi,m4v,ts` | Extensions counted as media (subs/nfo/art ignored) |
| `RUN_INTERVAL` | no | — | If set (e.g. `6h`), loop forever; otherwise run once and exit |
| `DRY_RUN` | no | `false` | Log planned triggers without POSTing (state file untouched) |
| `COOLDOWN_HOURS` | no | `24` | Don't re-trigger the same dir within this window |
| `STATE_FILE` | no | `/config/triggered.json` | Dir → timestamp map (mount with absolute host path!) |
| `PAGE_SIZE` | no | `500` | Plex pagination window |
| `BATCH_SIZE` | no | `50` | Max dirs per autoscan request (URL-length safety) |
| `MAX_TRIGGERS` | no | `200` | Max dirs triggered per run, stale first (`0` = unlimited). Paces the backlog so Plex/autoscan can absorb it; remainder next run |
| `MAX_STALE_PCT` | no | `25` | Abort the run if more than this % of Plex paths is missing (`100` = disable). Catches dead FUSE remounts where every symlink dangles — no triggers, no state write |

## Building locally

```bash
docker build -t plex-path-auditor:local .
```
