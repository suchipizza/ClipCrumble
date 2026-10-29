"""Hosting: rendered clips only (never source footage) -> Google Drive via rclone, public 'anyone with link'.

Behind one small interface so Dropbox can be added later. Requires a configured rclone remote (default `gdrive:`),
created once by the user with `rclone config` (browser login).
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path


class Hosting:
    def upload(self, local: Path, folder: str) -> dict: ...
    def ready(self) -> tuple[bool, str]: ...


class RcloneDrive(Hosting):
    def __init__(self, remote: str = "gdrive", root: str = "ClipCrumble"):
        self.remote = remote.rstrip(":")
        self.root = root

    def ready(self) -> tuple[bool, str]:
        r = subprocess.run(["rclone", "listremotes"], capture_output=True, text=True)
        if f"{self.remote}:" not in r.stdout.split():
            return False, (f"rclone remote '{self.remote}:' is not configured. Run `! rclone config` → n → name "
                           f"'{self.remote}' → storage 'drive' → scope 1 (full) → log in in the browser.")
        return True, "ok"

    def _path(self, folder: str, name: str = "") -> str:
        return f"{self.remote}:{self.root}/{folder}" + (f"/{name}" if name else "")

    def file_id(self, folder: str, name: str) -> str | None:
        r = subprocess.run(["rclone", "lsjson", self._path(folder), "--files-only"], capture_output=True, text=True)
        if r.returncode != 0:
            return None
        for e in json.loads(r.stdout or "[]"):
            if e["Name"] == name:
                return e.get("ID")
        return None

    def upload(self, local: Path, folder: str) -> dict:
        """Idempotent: rclone copy skips identical files (size+md5). Returns ids and links for every route."""
        subprocess.run(["rclone", "copy", str(local), self._path(folder), "--drive-acknowledge-abuse"], check=True,
                       capture_output=True, text=True)
        # anyone-with-link reader; rclone link creates the permission (least privilege first)
        r = subprocess.run(["rclone", "link", self._path(folder, local.name)], capture_output=True, text=True, check=True)
        fid = self.file_id(folder, local.name)
        return dict(drive_id=fid, share_url=r.stdout.strip(),
                    view_url=f"https://drive.google.com/file/d/{fid}/view?usp=sharing",
                    download_url=f"https://drive.google.com/uc?export=download&id={fid}")

    def delete_folder(self, folder: str):
        subprocess.run(["rclone", "purge", self._path(folder)], check=True)
