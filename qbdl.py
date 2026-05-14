#!/usr/bin/env python3

import argparse
import copy
import hashlib
import html
import importlib.util
import json
import os
import re
import runpy
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
import zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse


version = "1.000"
PROGRAM_NAME = "qbdl"
SCRIPT_PATH = Path(__file__).resolve()
APP_DIR = SCRIPT_PATH.parent
WINDOW_TITLE = f"{PROGRAM_NAME} v{version}"

UPDATE_REPOSITORY_URL = "https://github.com/linyv4ik/qbdl"
UPDATE_BRANCH = "main"
UPDATE_SCRIPT_PATH = SCRIPT_PATH.name
UPDATE_SKIP_PATH_PARTS = {"__pycache__"}


REQUIRED_PACKAGES = {
    "requests": "requests>=2.25.1",
    "mutagen": "mutagen>=1.45.1",
}


def ensure_dependencies() -> None:
    missing = [
        package
        for module, package in REQUIRED_PACKAGES.items()
        if importlib.util.find_spec(module) is None
    ]
    if not missing:
        return

    print("Installing missing Python packages: " + ", ".join(missing))
    commands = [
        [sys.executable, "-m", "pip", "install", *missing],
        [sys.executable, "-m", "pip", "install", "--user", *missing],
    ]

    try:
        subprocess.check_call([sys.executable, "-m", "pip", "--version"])
    except subprocess.CalledProcessError:
        try:
            subprocess.check_call([sys.executable, "-m", "ensurepip", "--upgrade"])
        except subprocess.CalledProcessError as error:
            raise RuntimeError("pip is not available and ensurepip failed") from error

    for command in commands:
        try:
            subprocess.check_call(command)
            break
        except subprocess.CalledProcessError:
            if command is commands[-1]:
                raise
    print("Dependencies installed. Continuing...")


ensure_dependencies()

import requests
from mutagen.flac import FLAC, Picture
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry


QUALITY_IDS = {
    "mp3": 5,
    "lossless": 6,
    "flac": 6,
    "hifi": 27,
    "hires": 27,
    "hi-res": 27,
}

DEFAULT_SETTINGS = {
    "download_path": "./downloads",
    "download_quality": "hifi",
    "album_folder_format": "{artist} - {album}{explicit} ({year})  [{quality}]",
    "track_filename_format": "{track_number}. {title}",
    "quality_format": "{bit_depth}B-{sample_rate}kHz",
    "artist_tag_separator": ", ",
    "embed_cover": True,
    "save_cover": True,
    "save_description": True,
    "skip_existing": True,
    "verify_tls": True,
    "request_timeout": 45,
    "download_threads": 3,
}

DEFAULT_CONFIG_DIR = APP_DIR / "config"
DEFAULT_URL_FILE = APP_DIR / "url.txt"
IGNORED_CONFIG_FILES = {"settings.json"}
DOWNLOAD_PRINT_LOCK = threading.Lock()


class QobuzError(RuntimeError):
    pass


def set_window_title(title: str) -> None:
    if os.name == "nt":
        try:
            import ctypes

            ctypes.windll.kernel32.SetConsoleTitleW(title)
        except Exception:
            pass


def window_title(context: str | None = None) -> str:
    context = (context or "").strip()
    if context:
        return f"{PROGRAM_NAME} {context}"
    return WINDOW_TITLE


def open_config_gui() -> None:
    for filename in ("config_gui.pyw", "config_gui.py"):
        path = APP_DIR / filename
        if path.exists():
            runpy.run_path(str(path), run_name="__main__")
            return
    raise QobuzError("qbdl config GUI not found")


def github_repo_parts(repository_url: str) -> tuple[str, str]:
    parsed = urlparse(repository_url.strip())
    parts = parsed.path.strip("/").split("/")
    if len(parts) < 2:
        raise QobuzError("UPDATE_REPOSITORY_URL must look like https://github.com/user/qbdl")
    owner = parts[0]
    repo = parts[1].removesuffix(".git")
    return owner, repo


def github_raw_url(path: str) -> str:
    owner, repo = github_repo_parts(UPDATE_REPOSITORY_URL)
    return f"https://raw.githubusercontent.com/{owner}/{repo}/{UPDATE_BRANCH}/{path}"


def github_zip_url() -> str:
    owner, repo = github_repo_parts(UPDATE_REPOSITORY_URL)
    return f"https://codeload.github.com/{owner}/{repo}/zip/refs/heads/{UPDATE_BRANCH}"


def read_url_text(url: str, timeout: int = 15) -> str:
    with urllib.request.urlopen(url, timeout=timeout) as response:
        return response.read().decode("utf-8-sig")


def parse_script_version(script_text: str) -> str | None:
    match = re.search(r"(?m)^\s*version\s*=\s*['\"]([^'\"]+)['\"]", script_text)
    return match.group(1).strip() if match else None


def version_parts(value: str) -> tuple[int, ...]:
    parts = re.findall(r"\d+", value)
    return tuple(int(part) for part in parts) if parts else (0,)


def remote_version() -> str | None:
    if not UPDATE_REPOSITORY_URL.strip():
        return None
    remote_script = read_url_text(github_raw_url(UPDATE_SCRIPT_PATH))
    return parse_script_version(remote_script)


def download_update_zip(target: Path) -> None:
    with urllib.request.urlopen(github_zip_url(), timeout=60) as response:
        with target.open("wb") as file:
            shutil.copyfileobj(response, file)


def should_skip_update_path(relative_path: Path) -> bool:
    if not relative_path.parts:
        return True
    return any(part in UPDATE_SKIP_PATH_PARTS for part in relative_path.parts)


def install_update_from_zip(zip_path: Path) -> None:
    app_root = APP_DIR.resolve()
    with tempfile.TemporaryDirectory(prefix=f"{PROGRAM_NAME}_update_") as temp_dir:
        extract_dir = Path(temp_dir) / "extract"
        extract_dir.mkdir()
        with zipfile.ZipFile(zip_path) as archive:
            archive.extractall(extract_dir)

        roots = [path for path in extract_dir.iterdir() if path.is_dir()]
        source_root = roots[0] if len(roots) == 1 else extract_dir

        for source in source_root.rglob("*"):
            relative_path = source.relative_to(source_root)
            if should_skip_update_path(relative_path):
                continue

            target = APP_DIR / relative_path
            resolved_target = target.resolve()
            if app_root != resolved_target and app_root not in resolved_target.parents:
                raise QobuzError(f"Refusing to write outside app folder: {target}")

            if source.is_dir():
                if target.exists() and not target.is_dir():
                    target.unlink()
                target.mkdir(parents=True, exist_ok=True)
            else:
                if target.exists() and target.is_dir():
                    shutil.rmtree(target)
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, target)


def restart_program() -> None:
    print(f"Restarting {PROGRAM_NAME}...")
    if os.name == "nt":
        helper_code = (
            "import os, time\n"
            "time.sleep(1)\n"
            f"os.startfile({SCRIPT_PATH.name!r}, cwd={str(APP_DIR)!r})\n"
        )
        subprocess.Popen(
            [sys.executable, "-c", helper_code],
            cwd=str(APP_DIR),
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
    else:
        subprocess.Popen(
            [sys.executable, str(SCRIPT_PATH)],
            cwd=str(APP_DIR),
            start_new_session=True,
        )
    raise SystemExit(0)


def check_for_updates() -> bool:
    if not UPDATE_REPOSITORY_URL.strip():
        return False

    try:
        available_version = remote_version()
    except Exception as error:
        print(f"Update check failed: {error}")
        return False

    if not available_version:
        print(f'Update check failed: version not found in remote {UPDATE_SCRIPT_PATH}')
        return False

    available_parts = version_parts(available_version)
    current_parts = version_parts(version)
    if available_parts < current_parts:
        print(f"GitHub version {available_version} is older than installed {version}. Update skipped.")
        return False
    if available_parts == current_parts:
        return False

    print(f"Current version: {version}")
    print(f"New version available: {available_version}")
    answer = input("Install update now? [y/N]: ").strip().lower()
    if answer not in {"y", "yes", "1", "так", "т"}:
        print("Update skipped.")
        return False

    with tempfile.TemporaryDirectory(prefix=f"{PROGRAM_NAME}_update_zip_") as temp_dir:
        zip_path = Path(temp_dir) / f"{PROGRAM_NAME}.zip"
        print("Downloading update...")
        download_update_zip(zip_path)
        print("Installing update...")
        install_update_from_zip(zip_path)

    print("Update installed.")
    restart_program()
    return True


@dataclass
class AccountConfig:
    country: str
    path: Path
    settings: dict[str, Any]


@dataclass
class AlbumMetadata:
    album_id: str
    title: str
    artist: str
    artist_id: str | None
    year: int | None
    release_date: str | None
    explicit: bool
    tracks: list[dict[str, Any]]
    quality: str
    cover_url: str | None
    description: str | None
    upc: str | None
    label: str | None
    copyright: str | None
    genre: str | None
    bit_depth: int
    sample_rate: float | int
    media_count: int | None
    tracks_count: int | None
    duration: int | None


@dataclass
class TrackMetadata:
    track_id: str
    title: str
    artists: list[str]
    album: AlbumMetadata
    track_number: int | None
    disc_number: int | None
    total_tracks: int | None
    total_discs: int | None
    composer: str | None
    isrc: str | None
    explicit: bool
    duration: int | None
    bit_depth: int | None
    sample_rate: float | int | None
    format_id: int | None
    download_url: str


def make_session() -> requests.Session:
    session = requests.Session()
    retries = Retry(
        total=10,
        backoff_factor=0.4,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=("GET",),
    )
    adapter = HTTPAdapter(max_retries=retries)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    return session


def md5(text: str) -> str:
    return hashlib.md5(text.encode("utf-8")).hexdigest()


def load_json(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8-sig") as file:
        return json.load(file)


def is_legacy_config(data: dict[str, Any]) -> bool:
    return "global" in data and "modules" in data


def active_enabled(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value == 1
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return False


def normalize_settings(data: dict[str, Any], country_name: str | None = None) -> dict[str, Any]:
    if is_legacy_config(data):
        global_settings = data.get("global", {})
        general = global_settings.get("general", {})
        formatting = global_settings.get("formatting", {})
        covers = global_settings.get("covers", {})
        advanced = global_settings.get("advanced", {})
        modules = data.get("modules", {})
        qobuz = modules.get("orpheusdl-qobuz", {})

        return {
            **DEFAULT_SETTINGS,
            "active": active_enabled(data.get("active", 0)),
            "country": country_name or data.get("country", ""),
            "download_path": general.get("download_path", DEFAULT_SETTINGS["download_path"]),
            "download_quality": general.get("download_quality", DEFAULT_SETTINGS["download_quality"]),
            "album_folder_format": formatting.get(
                "album_format", DEFAULT_SETTINGS["album_folder_format"]
            )
            .replace("{name}", "{album}")
            .replace("{release_year}", "{year}"),
            "track_filename_format": formatting.get(
                "track_filename_format", DEFAULT_SETTINGS["track_filename_format"]
            ).replace("{name}", "{title}"),
            "quality_format": qobuz.get("quality_format", DEFAULT_SETTINGS["quality_format"]),
            "artist_tag_separator": global_settings.get("tags", {}).get(
                "artist_tag_separator", DEFAULT_SETTINGS["artist_tag_separator"]
            ),
            "embed_cover": covers.get("embed_cover", DEFAULT_SETTINGS["embed_cover"]),
            "save_cover": True,
            "save_description": True,
            "skip_existing": not advanced.get("ignore_existing_files", False),
            "qobuz": {
                "app_id": qobuz.get("app_id", ""),
                "app_secret": qobuz.get("app_secret", ""),
                "user_id": qobuz.get("user_id", ""),
                "auth_token": qobuz.get("auth_token", ""),
            },
        }

    settings = {**DEFAULT_SETTINGS, **data}
    settings["active"] = active_enabled(data.get("active", 0))
    settings["country"] = data.get("country") or country_name or ""
    qobuz = settings.get("qobuz", {})
    if not qobuz:
        qobuz = {
            "app_id": settings.get("app_id", ""),
            "app_secret": settings.get("app_secret", ""),
            "user_id": settings.get("user_id", ""),
            "auth_token": settings.get("auth_token", ""),
        }
    settings["qobuz"] = qobuz
    return settings


def config_paths(config_location: str | None) -> list[Path]:
    location = Path(config_location) if config_location else DEFAULT_CONFIG_DIR
    if not location.is_absolute():
        location = APP_DIR / location
    if location.is_file():
        return [location]
    if location.is_dir():
        return sorted(
            path
            for path in location.glob("*.json")
            if path.name.lower() not in IGNORED_CONFIG_FILES
        )
    raise QobuzError(f'Config location not found: "{location}"')


def load_active_accounts(config_location: str | None) -> list[AccountConfig]:
    paths = config_paths(config_location)
    if not paths:
        base = Path(config_location) if config_location else DEFAULT_CONFIG_DIR
        raise QobuzError(f'No country configs found in "{base}"')

    accounts: list[AccountConfig] = []
    for path in paths:
        try:
            raw = load_json(path)
            settings = normalize_settings(raw, country_name=path.stem)
        except Exception as error:
            print(f'Skipping invalid config "{path}": {error}', file=sys.stderr)
            continue

        if not settings.get("active"):
            print(f"Skipping inactive config: {path.name}")
            continue

        country = str(settings.get("country") or path.stem)
        accounts.append(AccountConfig(country=country, path=path, settings=settings))

    if not accounts:
        raise QobuzError('No active configs found. Set "active": 1 in at least one config/*.json file.')
    return accounts


def sanitize_name(value: Any, fallback: str = "untitled") -> str:
    text = str(value or fallback).strip()
    text = re.sub(r"[:]", " - ", text)
    text = re.sub(r'[\\/*?"<>|$]', "", text)
    text = text.replace("\r", " ").replace("\n", " ").replace("\t", " ").strip(" .")
    return text or fallback


def truncate_component(value: str, byte_limit: int = 240) -> str:
    encoded = value.encode("utf-8")
    if len(encoded) <= byte_limit:
        return value
    return encoded[:byte_limit].decode("utf-8", "ignore").rstrip(" .")


def format_number(value: float | int | str | None) -> str:
    if value is None:
        return ""
    number = float(value)
    if number.is_integer():
        return str(int(number))
    return f"{number:g}"


def html_to_text(value: str | None) -> str | None:
    if not value:
        return None
    text = re.sub(r"(?i)<br\s*/?>", "\n", value)
    text = re.sub(r"(?i)</p\s*>", "\n\n", text)
    text = re.sub(r"<[^>]+>", "", text)
    text = html.unescape(text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def append_version(title: str, version: str | None) -> str:
    title = (title or "").rstrip()
    return f"{title} ({version})" if version else title


def original_cover_url(image_data: dict[str, Any] | None) -> str | None:
    if not image_data:
        return None
    url = image_data.get("large") or image_data.get("small") or image_data.get("thumbnail")
    if not url:
        return None
    return re.sub(r"_[^_/]+\.jpg$", "_org.jpg", url)


def parse_album_id(value: str) -> str:
    value = value.strip()
    if not value:
        raise QobuzError("Empty album URL/id")
    if not value.startswith("http"):
        return value

    parsed = urlparse(value)
    parts = [part for part in parsed.path.split("/") if part]
    if "album" not in parts or len(parts) < 2:
        raise QobuzError(f'Not a Qobuz album URL: "{value}"')
    return parts[-1]


def quality_id(setting: Any) -> int:
    if isinstance(setting, int):
        return setting
    text = str(setting).strip().lower()
    if text.isdigit():
        return int(text)
    if text not in QUALITY_IDS:
        raise QobuzError(
            f'Unknown quality "{setting}". Use one of: {", ".join(sorted(QUALITY_IDS))}'
        )
    return QUALITY_IDS[text]


def unique_names(names: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for name in names:
        clean = str(name or "").strip()
        key = clean.casefold()
        if clean and key not in seen:
            seen.add(key)
            result.append(clean)
    return result


def tag_value(value: Any) -> list[str] | None:
    if value is None or value == "":
        return None
    if isinstance(value, list):
        values = [str(item) for item in value if item is not None and str(item) != ""]
        return values or None
    return [str(value)]


def file_size_text(size: int | float | None) -> str:
    value = float(size or 0)
    units = ("B", "KB", "MB", "GB", "TB")
    for unit in units:
        if value < 1024 or unit == units[-1]:
            if unit == "B":
                return f"{int(value)}B"
            text = f"{value:.1f}".rstrip("0").rstrip(".")
            return f"{text}{unit}"
        value /= 1024
    return "0B"


def download_result_line(label: str, size: int | float | None, status: str) -> str:
    return f"{label}: {file_size_text(size)} {status}"


def path_size(path: Path) -> int:
    try:
        return path.stat().st_size
    except OSError:
        return 0


def print_download_result(label: str, size: int | float | None, status: str) -> None:
    with DOWNLOAD_PRINT_LOCK:
        print(download_result_line(label, size, status))


class QobuzClient:
    def __init__(self, settings: dict[str, Any]):
        qobuz = settings["qobuz"]
        self.api_base = "https://www.qobuz.com/api.json/0.2/"
        self.app_id = str(qobuz.get("app_id", "")).strip()
        self.app_secret = str(qobuz.get("app_secret", "")).strip()
        self.auth_token = str(qobuz.get("auth_token", "")).strip()
        self.verify_tls = bool(settings.get("verify_tls", True))
        self.timeout = int(settings.get("request_timeout", 45))
        self.session = make_session()
        self.session_lock = threading.Lock()

        missing = [
            name
            for name, value in {
                "app_id": self.app_id,
                "app_secret": self.app_secret,
                "auth_token": self.auth_token,
            }.items()
            if not value
        ]
        if missing:
            raise QobuzError("Missing Qobuz config values: " + ", ".join(missing))

    def headers(self) -> dict[str, str]:
        return {
            "X-Device-Platform": "android",
            "X-Device-Model": "Pixel 3",
            "X-Device-Os-Version": "10",
            "X-User-Auth-Token": self.auth_token,
            "X-Device-Manufacturer-Id": "482D8CB7-015D-402F-A93B-5EEF0E0996F3",
            "X-App-Version": "5.16.1.5",
            "User-Agent": (
                "Dalvik/2.1.0 (Linux; U; Android 10; Pixel 3 Build/QP1A.190711.020))"
                "QobuzMobileAndroid/5.16.1.5-b21041415"
            ),
        }

    def create_signature(self, method: str, parameters: dict[str, Any]) -> tuple[str, str]:
        timestamp = str(int(time.time()))
        to_hash = method.replace("/", "")
        for key in sorted(parameters.keys()):
            if key not in {"app_id", "user_auth_token"}:
                to_hash += key + str(parameters[key])
        to_hash += timestamp + self.app_secret
        return timestamp, md5(to_hash)

    def get(self, endpoint: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        params = params or {}
        with self.session_lock:
            response = self.session.get(
                self.api_base + endpoint,
                params=params,
                headers=self.headers(),
                timeout=self.timeout,
                verify=self.verify_tls,
            )
        if response.status_code not in {200, 201, 202}:
            raise QobuzError(f"Qobuz API error {response.status_code}: {response.text}")
        data = response.json()
        if isinstance(data, dict) and data.get("status") == "error":
            raise QobuzError(f'Qobuz API error: {data.get("message", data)}')
        return data

    def signed_get(self, endpoint: str, params: dict[str, Any]) -> dict[str, Any]:
        request_ts, request_sig = self.create_signature(endpoint, params)
        params = {**params, "request_ts": request_ts, "request_sig": request_sig}
        return self.get(endpoint, params)

    def check_token(self) -> str:
        data = self.signed_get("user/get", {"app_id": self.app_id})
        credential = data.get("credential", {})
        if credential.get("parameters"):
            return str(data.get("country", "unknown"))
        raise QobuzError("This Qobuz account is not eligible for downloads")

    def get_album(self, album_id: str) -> dict[str, Any]:
        return self.get(
            "album/get",
            {
                "album_id": album_id,
                "app_id": self.app_id,
                "extra": "albumsFromSameArtist,focusAll",
            },
        )

    def get_file_url(self, track_id: str, selected_quality_id: int) -> dict[str, Any]:
        params = {
            "track_id": track_id,
            "format_id": str(selected_quality_id),
            "intent": "stream",
            "sample": "false",
            "app_id": self.app_id,
            "user_auth_token": self.auth_token,
        }
        return self.signed_get("track/getFileUrl", params)

    def download(
        self,
        url: str,
        target: Path,
        label: str,
        overwrite: bool = False,
    ) -> bool:
        target.parent.mkdir(parents=True, exist_ok=True)
        partial = target.with_name(target.name + ".part")
        if partial.exists():
            partial.unlink()

        bytes_written = 0
        expected_size = 0
        existing_size = path_size(target) if target.is_file() else 0
        try:
            with make_session() as download_session:
                response = download_session.get(
                    url,
                    stream=True,
                    timeout=self.timeout,
                    verify=self.verify_tls,
                )
                response.raise_for_status()

                expected_size = int(response.headers.get("content-length", "0") or "0")
                if target.is_file() and not overwrite and (
                    expected_size == 0 or existing_size >= expected_size
                ):
                    print_download_result(label, existing_size, "already downloaded")
                    return False

                with partial.open("wb") as file:
                    for chunk in response.iter_content(chunk_size=1024 * 256):
                        if chunk:
                            file.write(chunk)
                            bytes_written += len(chunk)

            if expected_size and bytes_written < expected_size:
                raise QobuzError(
                    f"Incomplete download for {label}: "
                    f"{file_size_text(bytes_written)} of {file_size_text(expected_size)}"
                )
            os.replace(partial, target)
            print_download_result(label, expected_size or bytes_written, "done")
            return True
        except BaseException:
            print_download_result(label, bytes_written or expected_size, "fail")
            if partial.exists():
                partial.unlink()
            raise


class AlbumDownloader:
    def __init__(self, settings: dict[str, Any], client: QobuzClient):
        self.settings = settings
        self.client = client
        self.selected_quality_id = quality_id(settings["download_quality"])

    def album_metadata(self, album_id: str) -> AlbumMetadata:
        data = self.client.get_album(album_id)
        tracks = data.get("tracks", {}).get("items", [])
        bit_depth, sample_rate = self.album_quality(data)
        quality = self.settings["quality_format"].format(
            bit_depth=bit_depth,
            sample_rate=format_number(sample_rate),
        )
        release_date = data.get("release_date_original")
        year = int(release_date.split("-")[0]) if release_date else None
        label = data.get("label") or {}
        genre = data.get("genre") or {}

        return AlbumMetadata(
            album_id=str(data["id"]),
            title=append_version(data.get("title", ""), data.get("version")),
            artist=(data.get("artist") or {}).get("name", "Unknown Artist"),
            artist_id=str((data.get("artist") or {}).get("id") or ""),
            year=year,
            release_date=release_date,
            explicit=bool(data.get("parental_warning")),
            tracks=tracks,
            quality=quality,
            cover_url=original_cover_url(data.get("image")),
            description=html_to_text(data.get("description")),
            upc=data.get("upc"),
            label=label.get("name"),
            copyright=data.get("copyright"),
            genre=genre.get("name"),
            bit_depth=bit_depth,
            sample_rate=sample_rate,
            media_count=data.get("media_count"),
            tracks_count=data.get("tracks_count"),
            duration=data.get("duration"),
        )

    def album_quality(self, album_data: dict[str, Any]) -> tuple[int, float | int]:
        if self.selected_quality_id == 27 and album_data.get("hires_streamable"):
            bit_depth = album_data.get("maximum_bit_depth") or 24
            sample_rate = album_data.get("maximum_sampling_rate") or 44.1
            return int(bit_depth), sample_rate
        return 16, 44.1

    def track_metadata(
        self,
        raw_track: dict[str, Any],
        album: AlbumMetadata,
        stream: dict[str, Any] | None = None,
    ) -> TrackMetadata:
        track_id = str(raw_track["id"])
        title = append_version(raw_track.get("title", ""), raw_track.get("version"))
        if raw_track.get("work"):
            title = f'{raw_track["work"]} - {title}'

        main_artist = raw_track.get("performer") or {"name": album.artist, "id": album.artist_id}
        artists = [main_artist.get("name") or album.artist]
        performers = raw_track.get("performers")
        if performers:
            for credit in performers.split(" - "):
                pieces = [piece.strip() for piece in credit.split(", ") if piece.strip()]
                if len(pieces) < 2:
                    continue
                name, roles = pieces[0], set(pieces[1:])
                if roles & {"MainArtist", "FeaturedArtist", "Artist"}:
                    artists.append(name)

        composer = None
        if isinstance(raw_track.get("composer"), dict):
            composer = raw_track["composer"].get("name")

        download_url = ""
        format_id = 5 if self.selected_quality_id == 5 else self.selected_quality_id
        bit_depth = album.bit_depth
        sample_rate = album.sample_rate
        if stream is not None:
            download_url = stream.get("url") or ""
            if not download_url:
                raise QobuzError(f'No download URL returned for track "{raw_track.get("title", track_id)}"')
            format_id = stream.get("format_id")
            format_id = int(format_id) if format_id else None
            bit_depth = stream.get("bit_depth") or bit_depth
            sample_rate = stream.get("sampling_rate") or sample_rate

        return TrackMetadata(
            track_id=track_id,
            title=title,
            artists=unique_names(artists),
            album=album,
            track_number=raw_track.get("track_number"),
            disc_number=raw_track.get("media_number"),
            total_tracks=album.tracks_count,
            total_discs=album.media_count,
            composer=composer,
            isrc=raw_track.get("isrc"),
            explicit=bool(raw_track.get("parental_warning")),
            duration=raw_track.get("duration"),
            bit_depth=bit_depth,
            sample_rate=sample_rate,
            format_id=format_id,
            download_url=download_url,
        )

    def album_folder(self, album: AlbumMetadata, output_path: Path) -> Path:
        tags = {
            "id": album.album_id,
            "artist": sanitize_name(album.artist),
            "album": sanitize_name(album.title),
            "name": sanitize_name(album.title),
            "year": album.year or "",
            "release_year": album.year or "",
            "quality": sanitize_name(album.quality),
            "explicit": " [E]" if album.explicit else "",
        }
        folder_name = self.settings["album_folder_format"].format(**tags)
        folder_name = truncate_component(sanitize_name(folder_name))
        return output_path / folder_name

    def track_path(self, album_folder: Path, track: TrackMetadata) -> Path:
        number = str(track.track_number or 0)
        tags = {
            "track_number": number,
            "title": sanitize_name(track.title),
            "name": sanitize_name(track.title),
            "artist": sanitize_name(track.artists[0] if track.artists else track.album.artist),
            "album": sanitize_name(track.album.title),
            "year": track.album.year or "",
            "bit_depth": track.bit_depth or "",
            "sample_rate": format_number(track.sample_rate),
            "quality": track.album.quality,
            "explicit": " [E]" if track.explicit else "",
        }
        filename = self.settings["track_filename_format"].format(**tags)
        filename = truncate_component(sanitize_name(filename))
        extension = "mp3" if track.format_id == 5 else "flac"

        folder = album_folder
        if track.total_discs and track.total_discs > 1:
            folder = folder / f"CD {track.disc_number or 1}"
        return folder / f"{filename}.{extension}"

    def download_track_task(
        self,
        raw_track: dict[str, Any],
        album: AlbumMetadata,
        album_folder: Path,
        cover_path: Path,
        index: int,
        total_tracks: int,
    ) -> None:
        track = self.track_metadata(raw_track, album)
        target = self.track_path(album_folder, track)
        label = f"{index}. Track {index}/{total_tracks}: {track.title}"

        stream = self.client.get_file_url(track.track_id, self.selected_quality_id)
        track = self.track_metadata(raw_track, album, stream=stream)
        downloaded = self.client.download(
            track.download_url,
            target,
            label,
            overwrite=not self.settings.get("skip_existing", True),
        )
        if not downloaded:
            return

        if target.suffix.lower() == ".flac":
            self.tag_flac(target, track, cover_path if cover_path.exists() else None)

    def download_thread_count(self, total_tracks: int) -> int:
        try:
            configured_threads = int(self.settings.get("download_threads", 3))
        except (TypeError, ValueError):
            configured_threads = 3
        configured_threads = max(1, configured_threads)
        return max(1, min(configured_threads, total_tracks))

    def download_album(self, album_id: str, output_path: Path) -> Path:
        album = self.album_metadata(album_id)
        album_folder = self.album_folder(album, output_path)
        album_folder.mkdir(parents=True, exist_ok=True)

        print(f"Album: {album.artist} - {album.title} ({album.year or 'unknown'}) [{album.quality}]")
        print(f"Tracks: {len(album.tracks)}")
        print(f"Output: {album_folder}")

        cover_path = album_folder / "cover.jpg"
        if album.cover_url and self.settings.get("save_cover", True):
            self.client.download(
                album.cover_url,
                cover_path,
                "cover",
                overwrite=not self.settings.get("skip_existing", True),
            )

        if album.description and self.settings.get("save_description", True):
            description_path = album_folder / "description.txt"
            if not description_path.exists() or not self.settings.get("skip_existing", True):
                description_path.write_text(album.description + "\n", encoding="utf-8")

        total_tracks = len(album.tracks)
        download_threads = self.download_thread_count(total_tracks)
        with ThreadPoolExecutor(max_workers=download_threads) as executor:
            futures = [
                executor.submit(
                    self.download_track_task,
                    raw_track,
                    album,
                    album_folder,
                    cover_path,
                    index,
                    total_tracks,
                )
                for index, raw_track in enumerate(album.tracks, start=1)
            ]
            for future in as_completed(futures):
                future.result()

        return album_folder

    def artist_tag(self, artists: list[str]) -> list[str] | str:
        separator = self.settings.get("artist_tag_separator", ", ")
        if separator is None:
            return artists
        return str(separator).join(artists)

    def set_flac_tag(self, audio: FLAC, key: str, value: Any) -> None:
        normalized = tag_value(value)
        if normalized:
            audio[key] = normalized

    def tag_flac(self, path: Path, track: TrackMetadata, cover_path: Path | None) -> None:
        audio = FLAC(str(path))
        audio.clear()
        audio.clear_pictures()

        self.set_flac_tag(audio, "TITLE", track.title)
        self.set_flac_tag(audio, "ALBUM", track.album.title)
        self.set_flac_tag(audio, "ARTIST", self.artist_tag(track.artists))
        self.set_flac_tag(audio, "ALBUMARTIST", track.album.artist)
        self.set_flac_tag(audio, "DATE", track.album.release_date or track.album.year)
        self.set_flac_tag(audio, "TRACKNUMBER", track.track_number)
        self.set_flac_tag(audio, "TRACKTOTAL", track.total_tracks)
        self.set_flac_tag(audio, "TOTALTRACKS", track.total_tracks)
        self.set_flac_tag(audio, "DISCNUMBER", track.disc_number)
        self.set_flac_tag(audio, "DISCTOTAL", track.total_discs)
        self.set_flac_tag(audio, "TOTALDISCS", track.total_discs)
        self.set_flac_tag(audio, "GENRE", track.album.genre)
        self.set_flac_tag(audio, "COMPOSER", track.composer)
        self.set_flac_tag(audio, "ISRC", track.isrc)
        self.set_flac_tag(audio, "UPC", track.album.upc)
        self.set_flac_tag(audio, "BARCODE", track.album.upc)
        self.set_flac_tag(audio, "LABEL", track.album.label)
        self.set_flac_tag(audio, "COPYRIGHT", track.album.copyright)
        self.set_flac_tag(audio, "DESCRIPTION", track.album.description)
        self.set_flac_tag(audio, "RATING", "Explicit" if track.explicit else "Clean")
        self.set_flac_tag(audio, "QOBUZ_ALBUM_ID", track.album.album_id)
        self.set_flac_tag(audio, "QOBUZ_TRACK_ID", track.track_id)

        if cover_path and self.settings.get("embed_cover", True):
            picture = Picture()
            picture.type = 3
            picture.mime = "image/jpeg"
            picture.desc = "Cover"
            picture.data = cover_path.read_bytes()
            audio.add_picture(picture)

        audio.save()


def urls_from_arguments(values: list[str]) -> list[str]:
    urls: list[str] = []
    for value in values:
        path = Path(value)
        if not path.exists() and not path.is_absolute():
            app_path = APP_DIR / path
            if app_path.exists():
                path = app_path
        if path.exists() and path.is_file():
            lines = [
                line.strip()
                for line in path.read_text(encoding="utf-8-sig").splitlines()
                if line.strip() and not line.strip().startswith("#")
            ]
            urls.extend(lines)
        else:
            urls.append(value)
    return urls


def settings_with_overrides(
    base_settings: dict[str, Any],
    output_override: str | None,
    quality_override: str | None,
) -> dict[str, Any]:
    settings = copy.deepcopy(base_settings)
    if output_override:
        settings["download_path"] = output_override
    if quality_override:
        settings["download_quality"] = quality_override
    return settings


def download_album_with_accounts(
    accounts: list[AccountConfig],
    album_id: str,
    output_override: str | None,
    quality_override: str | None,
) -> Path:
    failures: list[str] = []

    for index, account in enumerate(accounts, start=1):
        settings = settings_with_overrides(account.settings, output_override, quality_override)
        set_window_title(window_title(account.country or account.path.stem))
        print(f"\nTrying config {index}/{len(accounts)}: {account.path.name}")

        try:
            client = QobuzClient(settings)
            country = client.check_token()
            print(f"Account region: {country}")

            output_path = Path(settings["download_path"])
            if not output_path.is_absolute():
                output_path = APP_DIR / output_path
            output_path.mkdir(parents=True, exist_ok=True)

            downloader = AlbumDownloader(settings, client)
            return downloader.download_album(album_id, output_path)
        except Exception as error:
            message = f"{account.path.name}: {error}"
            failures.append(message)
            print(f"Config failed: {message}", file=sys.stderr)

    raise QobuzError(
        f'All active configs failed for album "{album_id}".\n' + "\n".join(failures)
    )


def download_values(
    values: list[str],
    config_location: str | None,
    output_override: str | None,
    quality_override: str | None,
) -> None:
    accounts = load_active_accounts(config_location)
    for value in urls_from_arguments(values):
        album_id = parse_album_id(value)
        download_album_with_accounts(accounts, album_id, output_override, quality_override)


def clean_console_input(value: str) -> str:
    value = value.strip()
    for prefix in ("\ufeff", "ï»¿", "п»ї"):
        if value.startswith(prefix):
            value = value[len(prefix):].strip()
    return value


def interactive_mode(
    config_location: str | None,
    output_override: str | None,
    quality_override: str | None,
) -> int:
    while True:
        set_window_title(window_title())
        print()
        print("Введіть посилання на альбом Qobuz або виберіть бажану опцію з меню")
        print()
        if DEFAULT_URL_FILE.exists():
            print("1 Завантажити посилання з файлу url.txt")
            print()

        try:
            user_input = clean_console_input(input("> "))
        except EOFError:
            print()
            return 0

        if not user_input:
            continue
        if user_input.lower() in {"q", "quit", "exit", "e", "x", "вихід"}:
            return 0

        values = [str(DEFAULT_URL_FILE)] if user_input == "1" and DEFAULT_URL_FILE.exists() else [user_input]
        if user_input == "1" and not DEFAULT_URL_FILE.exists():
            print("Файл url.txt не знайдено.")
            continue

        try:
            download_values(values, config_location, output_override, quality_override)
        except Exception as error:
            print(f"Error: {error}", file=sys.stderr)


def main() -> int:
    set_window_title(window_title())
    parser = argparse.ArgumentParser(description="qbdl Qobuz album downloader")
    parser.add_argument("album", nargs="*", help="Qobuz album URL/id, or a text file with album URLs")
    parser.add_argument("-c", "--config", help="Config folder or file. Defaults to ./config")
    parser.add_argument("-o", "--output", help="Override download path")
    parser.add_argument("-q", "--quality", help="Override quality: hifi, lossless, mp3, or numeric Qobuz id")
    parser.add_argument("--config-gui", action="store_true", help="Open the config generator GUI")
    parser.add_argument("--no-update", action="store_true", help="Skip GitHub self-update check")
    args = parser.parse_args()
    set_window_title(window_title())

    if not args.no_update and check_for_updates():
        return 0

    if args.config_gui:
        open_config_gui()
        return 0

    if not args.album:
        return interactive_mode(args.config, args.output, args.quality)

    download_values(args.album, args.config, args.output, args.quality)

    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("\nAborted")
        raise SystemExit(130)
    except QobuzError as error:
        print(f"Error: {error}", file=sys.stderr)
        raise SystemExit(1)

