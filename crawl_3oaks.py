from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlparse

import requests


BASE_URL = "https://3oaks.com"
CATALOG_URL = f"{BASE_URL}/games?sort=release_date:asc"
API_URL = f"{BASE_URL}/api/v1/games"
DEFAULT_OUTPUT = Path("data") / "providers" / "3oaks"


def _safe_folder(value: str) -> str:
    clean = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", value).strip(" .")
    return clean[:140] or "game"


def _thumbnail_suffix(url: str) -> str:
    suffix = Path(urlparse(url).path).suffix.lower()
    if suffix in {".png", ".jpg", ".jpeg", ".webp", ".gif", ".avif"}:
        return suffix
    return ".img"


def _new_session() -> requests.Session:
    session = requests.Session()
    session.headers.update(
        {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128 Safari/537.36"
            ),
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "en-US,en;q=0.9",
            "Referer": CATALOG_URL,
            "Cache-Control": "no-cache",
        }
    )
    return session


def _fetch_page(
    session: requests.Session,
    page: int,
    *,
    page_size: int,
    timeout: float,
) -> tuple[list[dict[str, Any]], int]:
    response = session.get(
        API_URL,
        params={
            "page_num": page,
            "page_items_num": page_size,
            "game_name": "",
            "sort": "release_date:asc",
        },
        timeout=timeout,
    )
    response.raise_for_status()
    payload = response.json()

    data = payload.get("data")
    if not isinstance(data, dict):
        raise RuntimeError(f"3 Oaks: respuesta inválida en página {page}: falta data")

    items = data.get("items")
    total_pages = data.get("total_pages")
    if not isinstance(items, list):
        raise RuntimeError(f"3 Oaks: respuesta inválida en página {page}: items no es lista")
    try:
        total_pages_int = int(total_pages)
    except (TypeError, ValueError) as exc:
        raise RuntimeError(
            f"3 Oaks: respuesta inválida en página {page}: total_pages={total_pages!r}"
        ) from exc

    normalized = [item for item in items if isinstance(item, dict)]
    return normalized, max(1, total_pages_int)


def _download_thumbnail(
    session: requests.Session,
    url: str,
    target: Path,
    *,
    timeout: float,
) -> None:
    if target.is_file() and target.stat().st_size > 0:
        return

    response = session.get(url, timeout=timeout)
    response.raise_for_status()

    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(target.suffix + ".tmp")
    tmp.write_bytes(response.content)
    tmp.replace(target)


def crawl(output: Path, *, page_size: int = 15, timeout: float = 30.0) -> list[dict[str, str]]:
    output = output.resolve()
    output.mkdir(parents=True, exist_ok=True)

    session = _new_session()
    records_by_name: dict[str, dict[str, str]] = {}
    targets_by_name: dict[str, str] = {}

    try:
        page = 1
        total_pages = 1

        while page <= total_pages:
            items, observed_total_pages = _fetch_page(
                session,
                page,
                page_size=page_size,
                timeout=timeout,
            )
            if page == 1:
                total_pages = observed_total_pages
            elif observed_total_pages != total_pages:
                raise RuntimeError(
                    "3 Oaks: total_pages cambió durante el crawl "
                    f"({total_pages} -> {observed_total_pages})"
                )

            for item in items:
                name = str(item.get("title_text") or item.get("name") or "").strip()
                if not name:
                    continue

                slug = str(item.get("name") or "").strip()
                if slug:
                    targets_by_name[name.casefold()] = f"{API_URL}/{slug}/play?lang=en"

                raw_thumbnail = str(
                    item.get("main_logo_file")
                    or item.get("icon_file")
                    or ""
                ).strip()
                if not raw_thumbnail:
                    print(f"[sin miniatura] {name}")
                    continue

                thumbnail_url = urljoin(BASE_URL + "/", raw_thumbnail)
                game_dir = output / _safe_folder(name)
                thumbnail_path = game_dir / f"thumbnail{_thumbnail_suffix(thumbnail_url)}"

                _download_thumbnail(
                    session,
                    thumbnail_url,
                    thumbnail_path,
                    timeout=timeout,
                )

                records_by_name[name.casefold()] = {
                    "name": name,
                    "thumbnail": thumbnail_path.relative_to(output).as_posix(),
                }
                print(f"[ok] {name}")

            print(f"Página {page}/{total_pages}: acumulados={len(records_by_name)}")
            page += 1
    finally:
        session.close()

    records = sorted(records_by_name.values(), key=lambda row: row["name"].casefold())
    catalog_path = output / "catalog.json"
    tmp = catalog_path.with_suffix(".json.tmp")
    tmp.write_text(
        json.dumps(records, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    tmp.replace(catalog_path)

    targets_path = Path("targets.txt").resolve()
    targets = [targets_by_name[key] for key in sorted(targets_by_name)]
    targets_path.write_text("\n".join(targets) + ("\n" if targets else ""), encoding="utf-8")

    print(f"Listo: {len(records)} juegos")
    print(f"Catálogo: {catalog_path}")
    print(f"Targets: {targets_path} ({len(targets)} URLs)")
    return records


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Crawler mínimo del catálogo público de 3 Oaks: nombres + miniaturas."
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=DEFAULT_OUTPUT,
        help=f"Directorio de salida (default: {DEFAULT_OUTPUT.as_posix()})",
    )
    parser.add_argument(
        "--page-size",
        type=int,
        default=15,
        help="Cantidad pedida por página al API (default: 15; valor usado por 3 Oaks)",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=30.0,
        help="Timeout HTTP en segundos (default: 30)",
    )
    args = parser.parse_args()

    if args.page_size < 1:
        parser.error("--page-size debe ser >= 1")
    if args.timeout <= 0:
        parser.error("--timeout debe ser > 0")

    crawl(args.output, page_size=args.page_size, timeout=args.timeout)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
