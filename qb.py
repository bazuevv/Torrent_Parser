import re
import time
import threading
import requests
import config

_lock = threading.Lock()
_session: requests.Session | None = None

VIDEOS_DIR = "/home/ubuntu/Videos"

# Состояния, при которых торрент полностью скачан
COMPLETE_STATES = {"uploading", "stalledUP", "pausedUP", "forcedUP", "checkingUP", "queuedUP"}


def _new_session() -> requests.Session | None:
    s = requests.Session()
    try:
        resp = s.post(
            f"{config.QB_HOST}/api/v2/auth/login",
            data={"username": config.QB_USER, "password": config.QB_PASSWORD},
            headers={"Referer": config.QB_HOST, "Origin": config.QB_HOST},
            timeout=10,
        )
        if resp.text.strip() == "Ok.":
            return s
    except requests.RequestException:
        pass
    return None


def _get_session() -> requests.Session | None:
    global _session
    with _lock:
        if _session is not None:
            return _session
        _session = _new_session()
        return _session


def _invalidate():
    global _session
    with _lock:
        _session = None


def _api_get(path: str, params: dict | None = None) -> requests.Response | None:
    for attempt in range(2):
        s = _get_session()
        if s is None:
            return None
        try:
            resp = s.get(
                f"{config.QB_HOST}/api/v2/{path}",
                params=params,
                headers={"Referer": config.QB_HOST},
                timeout=10,
            )
            if resp.status_code == 403 and attempt == 0:
                _invalidate()
                continue
            return resp
        except requests.RequestException:
            return None
    return None


def extract_hash(magnet_uri: str) -> str | None:
    """Извлекает btih-хэш из магнет-ссылки."""
    m = re.search(r"urn:btih:([a-fA-F0-9]{40})", magnet_uri, re.IGNORECASE)
    return m.group(1).lower() if m else None


def get_torrents_info(hashes: list[str]) -> dict[str, dict]:
    """Возвращает словарь hash → torrent_info для переданного списка хэшей."""
    if not hashes:
        return {}
    resp = _api_get("torrents/info", {"hashes": "|".join(hashes)})
    if resp is None or resp.status_code != 200:
        return {}
    return {t["hash"].lower(): t for t in resp.json()}


def get_torrent_name_with_retry(torrent_hash: str, retries: int = 5, delay: float = 1.5) -> str | None:
    """Пытается получить имя торрента, ожидая пока qBittorrent получит метаданные."""
    for _ in range(retries):
        info = get_torrents_info([torrent_hash])
        if torrent_hash in info:
            name = info[torrent_hash].get("name", "")
            # qBittorrent возвращает хэш как имя пока не получил метаданные
            if name and name.lower() != torrent_hash:
                return name
        time.sleep(delay)
    return None


def file_url(torrent_info: dict) -> str | None:
    """Формирует URL для скачивания файла через Nginx /videos/."""
    content_path: str = torrent_info.get("content_path", "")
    if not content_path:
        return None
    if content_path.startswith(VIDEOS_DIR):
        rel = content_path[len(VIDEOS_DIR):]
        return f"/videos/{rel.lstrip('/')}"
    return None


def add_magnets_batch(magnet_uris: list[str], save_path: str | None = None) -> tuple[bool, str]:
    """Добавляет несколько магнет-ссылок одним запросом (URLs через \\n)."""
    if not magnet_uris:
        return True, "Нет ссылок для добавления"
    for attempt in range(2):
        s = _get_session()
        if s is None:
            return False, "Не удалось авторизоваться в qBittorrent"
        data: dict = {"urls": "\n".join(magnet_uris)}
        if save_path:
            data["savepath"] = save_path
        try:
            resp = s.post(
                f"{config.QB_HOST}/api/v2/torrents/add",
                data=data,
                headers={"Referer": config.QB_HOST, "Origin": config.QB_HOST},
                timeout=30,
            )
            if resp.status_code == 403 and attempt == 0:
                _invalidate()
                continue
            if resp.text.strip() == "Ok.":
                return True, f"Добавлено {len(magnet_uris)} торрентов"
            return False, f"qBittorrent ответил: {resp.text.strip()}"
        except requests.RequestException as exc:
            return False, f"Ошибка соединения: {exc}"
    return False, "Не удалось добавить после повторной авторизации"


def add_magnet(magnet_uri: str, save_path: str | None = None) -> tuple[bool, str]:
    """Добавляет магнет-ссылку в qBittorrent. Возвращает (успех, сообщение)."""
    for attempt in range(2):
        s = _get_session()
        if s is None:
            return False, "Не удалось авторизоваться в qBittorrent"

        data: dict = {"urls": magnet_uri}
        if save_path:
            data["savepath"] = save_path

        try:
            resp = s.post(
                f"{config.QB_HOST}/api/v2/torrents/add",
                data=data,
                headers={"Referer": config.QB_HOST, "Origin": config.QB_HOST},
                timeout=10,
            )
            if resp.status_code == 403 and attempt == 0:
                _invalidate()
                continue
            if resp.text.strip() == "Ok.":
                return True, "Торрент добавлен в qBittorrent"
            return False, f"qBittorrent ответил: {resp.text.strip()}"
        except requests.RequestException as exc:
            return False, f"Ошибка соединения с qBittorrent: {exc}"

    return False, "Не удалось добавить торрент после повторной авторизации"
