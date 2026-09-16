"""Plex and Autoscan API clients."""

import logging
import xml.etree.ElementTree as ET

import requests

log = logging.getLogger(__name__)

# Plex media type integers (GET /library/sections/{id}/all?type=)
MOVIE = 1
EPISODE = 4


class PlexClient:
    """Client for Plex library enumeration (read-only)."""

    def __init__(self, url: str, token: str, page_size: int = 500):
        self.url = url.rstrip("/")
        self.token = token
        self.page_size = page_size

    def _get(self, path: str, **params) -> bytes:
        params["X-Plex-Token"] = self.token
        resp = requests.get(f"{self.url}{path}", params=params, timeout=30)
        resp.raise_for_status()
        return resp.content

    def get_sections(self) -> list[dict]:
        """Return all library sections as {key, type, title} dicts."""
        root = ET.fromstring(self._get("/library/sections"))
        out = []
        for d in root.iter("Directory"):
            try:
                out.append({
                    "key": d.attrib["key"],
                    "type": d.attrib.get("type", ""),
                    "title": d.attrib.get("title", ""),
                })
            except KeyError:
                continue
        return out

    def list_section_files(self, section_id: str | int, media_type: int) -> list[str]:
        """Return every Part.file path in a section (paginated by item offset).

        The offset counts ITEMS (Videos), not files: multi-part items have
        several files per item and multi-episode files share one file across
        items, so advancing by file count skips items. Also, a missing
        totalSize must not end the walk after one page.
        """
        files: list[str] = []
        start = 0
        while True:
            content = self._get(
                f"/library/sections/{section_id}/all",
                type=media_type,
                **{"X-Plex-Container-Start": start, "X-Plex-Container-Size": self.page_size},
            )
            root = ET.fromstring(content)
            videos = list(root.iter("Video"))
            if not videos:
                break
            for video in videos:
                for media in video.iter("Media"):
                    for part in media.iter("Part"):
                        f = part.attrib.get("file")
                        if f:
                            files.append(f)
            start += len(videos)
            total_raw = root.attrib.get("totalSize")
            if total_raw is not None and start >= int(total_raw):
                break
        return files


class SonarrClient:
    """Client for Sonarr API (hash-file resolution + rename)."""

    def __init__(self, url: str, api_key: str):
        self.url = url.rstrip("/")
        self.headers = {"X-Api-Key": api_key}

    def get_naming(self) -> dict:
        """Return Sonarr naming config (renameEpisodes bool). Empty dict on failure."""
        try:
            resp = requests.get(f"{self.url}/api/v3/config/naming", headers=self.headers, timeout=30)
            resp.raise_for_status()
            data = resp.json()
            return data if isinstance(data, dict) else {}
        except Exception as e:
            log.debug(f"sonarr naming config failed: {e}")
            return {}

    def get_series(self) -> list[dict]:
        """Return all series (each with id, title, path)."""
        resp = requests.get(f"{self.url}/api/v3/series", headers=self.headers, timeout=30)
        resp.raise_for_status()
        data = resp.json()
        return data if isinstance(data, list) else []

    def get_renames(self, series_id: int, season_number: int) -> list[dict]:
        """Preview renames for a season: episodeFileId, existingPath, newPath."""
        resp = requests.get(
            f"{self.url}/api/v3/rename",
            params={"seriesId": series_id, "seasonNumber": season_number},
            headers=self.headers,
            timeout=30,
        )
        resp.raise_for_status()
        data = resp.json()
        return data if isinstance(data, list) else []

    def rename_files(self, series_id: int, file_ids: list[int]) -> bool:
        """Queue a RenameFiles command for episode file IDs. True on 2xx."""
        try:
            resp = requests.post(
                f"{self.url}/api/v3/command",
                headers=self.headers,
                json={"name": "RenameFiles", "seriesId": series_id, "files": file_ids},
                timeout=30,
            )
            if 200 <= resp.status_code < 300:
                return True
            log.warning(f"sonarr rename command returned {resp.status_code}: {resp.text[:200]}")
            return False
        except Exception as e:
            log.warning(f"sonarr rename command failed: {e}")
            return False


class AutoscanClient:
    """Client for Autoscan manual trigger (POST /triggers/manual?dir=...)."""

    def __init__(self, url: str, username: str, password: str):
        self.url = url.rstrip("/")
        self.auth = (username, password)

    def trigger_dirs(self, dirs: list[str]) -> bool:
        """Fire one manual scan request for a batch of directories. True on 2xx."""
        try:
            resp = requests.post(
                f"{self.url}/triggers/manual",
                params=[("dir", d) for d in dirs],
                auth=self.auth,
                timeout=30,
            )
            if 200 <= resp.status_code < 300:
                return True
            log.warning(f"autoscan returned {resp.status_code}: {resp.text[:200]}")
            return False
        except Exception as e:
            log.warning(f"autoscan request failed: {e}")
            return False
