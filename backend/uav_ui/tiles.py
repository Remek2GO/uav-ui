"""Map sources and the on-disk tile cache.

Only sources whose terms allow keeping tiles get a disk cache and area download. Everything else is
loaded by the browser directly from the provider.
"""

import asyncio
import math
from pathlib import Path

import httpx

SOURCES = {
    "orto": {
        "id": "orto",
        "label": "Ortofotomapa (Polska, offline)",
        "offline": True,
        "attribution": "Ortofotomapa: GUGiK, geoportal.gov.pl",
        "max_zoom": 19,
        "remote": (
            "https://mapy.geoportal.gov.pl/wss/service/PZGIK/ORTO/WMTS/StandardResolution"
            "?SERVICE=WMTS&REQUEST=GetTile&VERSION=1.0.0&LAYER=ORTOFOTOMAPA&STYLE=default"
            "&FORMAT=image/jpeg&TILEMATRIXSET=EPSG:3857&TILEMATRIX=EPSG:3857:{z}"
            "&TILEROW={y}&TILECOL={x}"
        ),
    },
    "esri": {
        "id": "esri",
        "label": "Esri World Imagery (tylko online)",
        "offline": False,
        "attribution": "Esri, Maxar, Earthstar Geographics",
        "max_zoom": 19,
        "direct": (
            "https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer"
            "/tile/{z}/{y}/{x}"
        ),
    },
}

MAX_PREFETCH_TILES = 3000
PREFETCH_CONCURRENCY = 4
PREFETCH_MAX_ZOOM = 19
REQUEST_TIMEOUT_S = 20


class TileError(Exception):
    pass


def tiles_in_bbox(west, south, east, north, min_zoom, max_zoom):
    """Yields (z, x, y) covering a WGS84 box. `max_zoom` is inclusive."""
    for zoom in range(min_zoom, max_zoom + 1):
        x0, y0 = _tile_of(west, north, zoom)
        x1, y1 = _tile_of(east, south, zoom)
        for x in range(x0, x1 + 1):
            for y in range(y0, y1 + 1):
                yield zoom, x, y


def count_tiles(west, south, east, north, min_zoom, max_zoom):
    total = 0
    for zoom in range(min_zoom, max_zoom + 1):
        x0, y0 = _tile_of(west, north, zoom)
        x1, y1 = _tile_of(east, south, zoom)
        total += (x1 - x0 + 1) * (y1 - y0 + 1)
    return total


def _tile_of(lon, lat, zoom):
    n = 2**zoom
    x = int((lon + 180.0) / 360.0 * n)
    lat_rad = math.radians(max(min(lat, 85.0511), -85.0511))
    y = int((1.0 - math.asinh(math.tan(lat_rad)) / math.pi) / 2.0 * n)
    return min(max(x, 0), n - 1), min(max(y, 0), n - 1)


class TileCache:
    def __init__(self, data_dir: Path):
        self.root = data_dir / "tiles"
        self.client = None
        self.jobs = {}
        self._job_counter = 0
        self._tasks = set()

    async def start(self):
        self.client = httpx.AsyncClient(
            timeout=REQUEST_TIMEOUT_S, headers={"User-Agent": "uav-ui"}, follow_redirects=True
        )

    async def stop(self):
        for task in list(self._tasks):
            task.cancel()
        if self.client is not None:
            await self.client.aclose()

    def public_sources(self):
        """What the browser needs. Sources without a direct URL go through this backend."""
        result = []
        for source in SOURCES.values():
            result.append(
                {
                    "id": source["id"],
                    "label": source["label"],
                    "offline": source["offline"],
                    "attribution": source["attribution"],
                    "max_zoom": source["max_zoom"],
                    "tiles": source.get("direct", f"/tiles/{source['id']}/{{z}}/{{x}}/{{y}}"),
                }
            )
        return result

    def _path(self, source_id, z, x, y):
        return self.root / source_id / str(z) / str(x) / f"{y}.jpg"

    async def get_tile(self, source_id, z, x, y):
        """Returns tile bytes, from disk when possible. None when the provider has no such tile."""
        source = SOURCES.get(source_id)
        if source is None or not source["offline"]:
            raise TileError("tego podkładu nie obsługuje backend")
        n = 2**z
        if not (0 <= z <= source["max_zoom"] and 0 <= x < n and 0 <= y < n):
            raise TileError("kafelek poza zakresem")
        path = self._path(source_id, z, x, y)
        if path.is_file():
            return path.read_bytes()
        url = source["remote"].format(z=z, x=x, y=y)
        response = await self.client.get(url)
        if response.status_code >= 500:
            # The map does not ask again for a failed tile, so one busy moment would leave a hole.
            await asyncio.sleep(0.5)
            response = await self.client.get(url)
        if response.status_code != 200 or not response.headers.get("content-type", "").startswith(
            "image/"
        ):
            return None
        data = response.content
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(".tmp")
        temporary.write_bytes(data)
        temporary.replace(path)
        return data

    def start_prefetch(self, source_id, west, south, east, north, min_zoom, max_zoom):
        source = SOURCES.get(source_id)
        if source is None or not source["offline"]:
            raise TileError("tego podkładu nie można pobrać do pracy offline")
        if not (-180 <= west < east <= 180 and -85 <= south < north <= 85):
            raise TileError("nieprawidłowy obszar")
        if not 0 <= min_zoom <= max_zoom <= min(PREFETCH_MAX_ZOOM, source["max_zoom"]):
            raise TileError("nieprawidłowy zakres przybliżenia")
        total = count_tiles(west, south, east, north, min_zoom, max_zoom)
        if total > MAX_PREFETCH_TILES:
            raise TileError(
                f"Ten obszar to {total} kafelków, a limit wynosi {MAX_PREFETCH_TILES}. "
                "Przybliż mapę, żeby pobrać mniejszy obszar."
            )
        self._job_counter += 1
        job = {"id": self._job_counter, "total": total, "done": 0, "failed": 0, "finished": False}
        self.jobs[job["id"]] = job
        task = asyncio.create_task(
            self._prefetch(job, source_id, (west, south, east, north, min_zoom, max_zoom))
        )
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return job

    async def _prefetch(self, job, source_id, area):
        semaphore = asyncio.Semaphore(PREFETCH_CONCURRENCY)

        async def fetch(z, x, y):
            async with semaphore:
                try:
                    if await self.get_tile(source_id, z, x, y) is None:
                        job["failed"] += 1
                except (httpx.HTTPError, TileError, OSError):
                    job["failed"] += 1
                job["done"] += 1

        try:
            await asyncio.gather(*(fetch(z, x, y) for z, x, y in tiles_in_bbox(*area)))
        finally:
            job["finished"] = True
