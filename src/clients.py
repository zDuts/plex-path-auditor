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
        """Return every Part.file path in a section (paginated)."""
        files: list[str] = []
        start = 0
        while True:
            content = self._get(
                f"/library/sections/{section_id}/all",
                type=media_type,
                **{"X-Plex-Container-Start": start, "X-Plex-Container-Size": self.page_size},
            )
            root = ET.fromstring(content)
            container = root
            batch = 0
            for video in container.iter("Video"):
                for media in video.iter("Media"):
                    for part in media.iter("Part"):
                        f = part.attrib.get("file")
                        if f:
                            files.append(f)
                            batch += 1
            total = int(container.attrib.get("totalSize", start + batch))
            start += batch
            # totalSize==0 can lie on some builds; stop when a page comes back empty
            if batch == 0 or start >= total:
                break
        return files


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
