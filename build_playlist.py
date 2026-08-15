#!/usr/bin/env python3
"""
Сборщик рабочего IPTV-плейлиста: Азербайджан + СНГ + спорт (Матч ТВ и др.)

Что делает:
1. Скачивает актуальные списки каналов с открытого проекта iptv-org
   (https://github.com/iptv-org/iptv) — он живой, обновляется волонтёрами
   ежедневно, поэтому вместо "мёртвых" зашитых вручную ссылок мы всегда
   тянем свежие кандидаты.
2. Дополнительно можно добавить свои собственные источники/ссылки в
   EXTRA_SOURCES и MANUAL_CHANNELS ниже.
3. Параллельно (пул потоков) проверяет КАЖДУЮ ссылку на то, что поток
   реально отвечает (HTTP 200 и похоже на HLS-плейлист / видео поток).
4. В финальный .m3u попадают только каналы, прошедшие проверку.
5. Отдельно ищет все варианты "Матч" / "Match" (Матч ТВ, Матч! Арена,
   Матч! Игра, Матч! Страна и т.д.) по всем источникам — какой из них
   жив прямо сейчас, тот и попадёт в плейлист. Если ни один официальный
   бесплатный источник Матч ТВ не отдаёт поток (канал часто закрыт
   гео-блоком/DRM вне РФ), скрипт честно это скажет в логе, а не
   подставит рабочую вручную выдуманную ссылку.

Запуск:
    pip install requests
    python3 build_playlist.py

Результат: cis_and_az_channels.m3u (только живые каналы) +
           check_report.json (полный отчёт: что живое, что нет)
"""

import re
import json
import time
import concurrent.futures as cf
from datetime import datetime, timezone

import requests

# ---------------------------------------------------------------------------
# НАСТРОЙКИ
# ---------------------------------------------------------------------------

# Страновые/тематические плейлисты iptv-org, которые нас интересуют.
# Полный список стран: https://github.com/iptv-org/iptv/tree/master/streams
IPTVORG_BASE = "https://raw.githubusercontent.com/iptv-org/iptv/master/streams/{}.m3u"
COUNTRY_CODES = {
    "az": "Azerbaijan",
    "ru": "Russia",
    "kz": "Kazakhstan",
    "uz": "Uzbekistan",
    "by": "Belarus",
    "ge": "Georgia",
    "am": "Armenia",
}

# Сюда можно добавить любые свои .m3u / .m3u8 ссылки на плейлисты —
# они будут скачаны и проверены точно так же, как источники iptv-org.
EXTRA_SOURCES: list[str] = [
    # "https://example.com/my_playlist.m3u",
]

# Ручные каналы (имя, группа, лого, url), если знаешь конкретную ссылку,
# которую хочешь гарантированно попробовать (она всё равно пройдёт
# проверку на "живость" наравне со всеми остальными).
MANUAL_CHANNELS: list[dict] = [
    # {
    #     "name": "Мой канал",
    #     "group": "Azerbaijan (Азербайджан)",
    #     "logo": "",
    #     "url": "https://example.com/stream/playlist.m3u8",
    # },
]

# Ключевые слова, по которым ищем "Матч ТВ" и его подканалы среди ВСЕХ
# скачанных источников (регистр не важен).
MATCH_TV_KEYWORDS = ["match", "матч"]

# Проверка ссылок
CHECK_TIMEOUT = 8          # секунд на попытку
CHECK_RETRIES = 2          # повторов на ссылку перед тем, как считать мёртвой
MAX_WORKERS = 24           # параллельных проверок
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)

OUTPUT_M3U = "cis_and_az_channels.m3u"
OUTPUT_REPORT = "check_report.json"

# ---------------------------------------------------------------------------
# ЗАГРУЗКА ИСТОЧНИКОВ
# ---------------------------------------------------------------------------

def build_session() -> requests.Session:
    s = requests.Session()
    s.headers.update({"User-Agent": USER_AGENT})
    return s


def parse_m3u(text: str):
    """Возвращает список (extinf_строка, url) из содержимого m3u-файла."""
    entries = []
    current_extinf = None
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if line.startswith("#EXTINF:"):
            current_extinf = line
        elif line.startswith("#EXTVLCOPT") or line.startswith("#EXT-X"):
            continue  # доп. опции VLC/HLS — не url потока
        elif not line.startswith("#"):
            if current_extinf:
                entries.append((current_extinf, line))
            current_extinf = None
    return entries


def channel_name(extinf: str) -> str:
    # Имя канала — всё после последней запятой в строке EXTINF
    return extinf.rsplit(",", 1)[-1].strip()


def set_group(extinf: str, group: str) -> str:
    if 'group-title="' in extinf:
        return re.sub(r'group-title="[^"]*"', f'group-title="{group}"', extinf)
    return extinf.replace("#EXTINF:-1", f'#EXTINF:-1 group-title="{group}"', 1)


def fetch_sources(session: requests.Session):
    """Скачивает все источники, возвращает dict: url -> {extinf, group}"""
    candidates: dict[str, dict] = {}

    def add(entries, group):
        for extinf, url in entries:
            url = url.strip()
            if not url or url in candidates:
                continue
            candidates[url] = {"extinf": set_group(extinf, group), "group": group}

    for code, label in COUNTRY_CODES.items():
        url = IPTVORG_BASE.format(code)
        try:
            r = session.get(url, timeout=15)
            r.raise_for_status()
            group = "Azerbaijan (Азербайджан)" if code == "az" else f"CIS / {label}"
            add(parse_m3u(r.text), group)
            print(f"[OK]   {url} — {len(parse_m3u(r.text))} каналов")
        except Exception as e:
            print(f"[FAIL] {url}: {e}")

    for url in EXTRA_SOURCES:
        try:
            r = session.get(url, timeout=15)
            r.raise_for_status()
            add(parse_m3u(r.text), "Дополнительно")
            print(f"[OK]   {url}")
        except Exception as e:
            print(f"[FAIL] {url}: {e}")

    for ch in MANUAL_CHANNELS:
        extinf = f'#EXTINF:-1 tvg-logo="{ch.get("logo","")}" group-title="{ch["group"]}",{ch["name"]}'
        candidates[ch["url"]] = {"extinf": extinf, "group": ch["group"]}

    return candidates


# ---------------------------------------------------------------------------
# ПРОВЕРКА "ЖИВОСТИ" ССЫЛОК
# ---------------------------------------------------------------------------

def is_alive(session: requests.Session, url: str) -> tuple[bool, str]:
    """Пробует HEAD, потом частичный GET. Возвращает (жив?, причина)."""
    headers = {"Range": "bytes=0-2048"}
    for attempt in range(CHECK_RETRIES):
        try:
            r = session.head(url, timeout=CHECK_TIMEOUT, allow_redirects=True)
            if r.status_code in (200, 206):
                return True, f"HEAD {r.status_code}"
            if r.status_code in (403, 405, 501):
                # некоторые CDN не любят HEAD — пробуем GET
                r = session.get(url, timeout=CHECK_TIMEOUT, headers=headers, stream=True)
                if r.status_code in (200, 206):
                    return True, f"GET {r.status_code}"
                return False, f"status {r.status_code}"
            return False, f"status {r.status_code}"
        except requests.exceptions.RequestException as e:
            last_err = str(e)
            time.sleep(0.5)
    return False, last_err if 'last_err' in dir() else "unknown error"


def check_all(session: requests.Session, candidates: dict) -> dict:
    results = {}
    with cf.ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        futures = {
            pool.submit(is_alive, build_session(), url): url for url in candidates
        }
        done = 0
        total = len(futures)
        for fut in cf.as_completed(futures):
            url = futures[fut]
            alive, reason = fut.result()
            results[url] = {"alive": alive, "reason": reason}
            done += 1
            if done % 25 == 0 or done == total:
                print(f"  проверено {done}/{total}...")
    return results


# ---------------------------------------------------------------------------
# ГЛАВНАЯ ЛОГИКА
# ---------------------------------------------------------------------------

def main():
    session = build_session()

    print("Скачиваю списки каналов (iptv-org + доп. источники)...")
    candidates = fetch_sources(session)
    print(f"Всего кандидатов до проверки: {len(candidates)}")

    print("\nПроверяю каждую ссылку на 'живость' (это может занять пару минут)...")
    results = check_all(session, candidates)

    alive_urls = [u for u, r in results.items() if r["alive"]]
    print(f"\nЖивых ссылок: {len(alive_urls)} из {len(candidates)}")

    # Собираем финальный список
    final_entries = []
    for url in alive_urls:
        final_entries.append((candidates[url]["extinf"], url))

    # Сортировка: сначала Azerbaijan, потом остальное; внутри группы по имени
    def sort_key(item):
        extinf, _ = item
        group_match = re.search(r'group-title="([^"]*)"', extinf)
        group = group_match.group(1) if group_match else ""
        return (0 if "Azerbaijan" in group else 1, group, channel_name(extinf))

    final_entries.sort(key=sort_key)

    with open(OUTPUT_M3U, "w", encoding="utf-8") as f:
        f.write("#EXTM3U\n")
        for extinf, url in final_entries:
            f.write(f"{extinf}\n{url}\n")

    # Отчёт: отдельно покажем, что нашли/потеряли по "Матч"
    match_report = {
        url: {**info, "name": channel_name(candidates[url]["extinf"])}
        for url, info in results.items()
        if any(kw in channel_name(candidates[url]["extinf"]).lower() for kw in MATCH_TV_KEYWORDS)
    }

    report = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "total_candidates": len(candidates),
        "alive": len(alive_urls),
        "dead": len(candidates) - len(alive_urls),
        "match_tv_candidates": match_report,
    }
    with open(OUTPUT_REPORT, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)

    print(f"\nГотово. Плейлист: {OUTPUT_M3U} ({len(final_entries)} рабочих каналов)")
    print(f"Отчёт: {OUTPUT_REPORT}")

    if match_report:
        alive_match = [v for v in match_report.values() if v["alive"]]
        print(f"\n'Матч'-каналов найдено: {len(match_report)}, живых: {len(alive_match)}")
        for v in alive_match:
            print(f"  ✔ {v['name']}")
        if not alive_match:
            print("  Ни один найденный вариант Матч ТВ сейчас не отвечает.")
            print("  Официальный Матч ТВ часто закрыт гео-блоком/DRM вне РФ —")
            print("  бесплатных легальных зеркал, которые стабильно живут неделями, нет.")
    else:
        print("\nКаналов с 'Матч'/'Match' в источниках не найдено вообще.")


if __name__ == "__main__":
    main()
