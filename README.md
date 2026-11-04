# uav-ui

Lokalne GUI do dronów: mapa satelitarna z pozycją drona i własnymi obiektami, obraz na żywo, stan i komendy lotu, planowanie punktów i ścieżek, pluginy projektów.

Działa bez drona i bez pluginu. Połączenie i plugin są opcjonalne: sama mapa i plany działają od razu.

## Uruchomienie

```bash
docker compose up -d --build
```

Otwórz http://127.0.0.1:8090. Wszystko nasłuchuje tylko na `127.0.0.1`. Do działania nie jest potrzebny ROS na laptopie.

## Co czego używa

```text
przeglądarka ── REST/WS/WHEP ─▶ backend FastAPI (127.0.0.1:8090, serwuje też frontend)
                                 ├─ MAVLink UDP 14570 ⇄ wfb-gs, przekazywany do QGC na 14550
                                 ├─ wideo UDP 5610 → MediaMTX (WebRTC), przekazywane do QGC na 5600
                                 ├─ cache kafelków, plany, konfiguracja pluginów (./data)
                                 └─ HTTP przez tunel ▶ 10.5.0.2:8765 uav_ui_bridge ▶ topiki lokalne drona
```

| Katalog | Co to jest |
|---|---|
| `backend/` | FastAPI, MAVLink, klient bridge'a, pluginy, plany, kafelki |
| `frontend/` | React, TypeScript, MapLibre, dockview |
| `bridge/uav_ui_bridge/` | Węzeł ROS 2 na dronie. Wykonuje tylko to, co jest w pliku pluginu |
| `spec/` | [Format pluginu](spec/plugin-format.md), [API bridge'a](spec/bridge-api.md), [API panelu własnego](spec/custom-panel-api.md) |
| `plugins/example/` | Plugin pokazujący wszystkie możliwości |

## Praca z dronem przez WFB

Domyślny profil **WFB (dron)** wymaga, żeby `wfb-gs` wysyłał dane do GUI, a nie bezpośrednio do QGC. W `wfb-gs/.env`:

```bash
VIDEO_OUT=127.0.0.1:5610
MAVLINK_OUT=127.0.0.1:14570
```

Po zmianie `docker compose up -d` w `wfb-gs`. GUI przekazuje dane dalej, więc QGC działa obok bez zmian w ustawieniach. **Bez uruchomionego GUI QGC nic nie dostanie.** Żeby używać samego QGC, przywróć `5600` i `14550`.

Na dronie obraz `wfb-uav` musi być przebudowany, bo blokuje porty DDS na tunelu (`TUNNEL_BLOCK_DDS`). Inaczej ROS laptopa i drona łączą się w jeden graf.

## Symulacja

Profil **Symulacja** słucha PX4 SITL na porcie 14540, więc QGC może zająć port 14550 w tym samym czasie. Bridge musi działać na `127.0.0.1:8765`.

Plugin przykładowy bez drona, w środowisku ROS 2 ze zbudowanym pakietem `uav_ui_bridge`:

```bash
bridge/uav_ui_bridge/test/run_example.sh plugins/example
```

## ⚠️ Bezpieczeństwo

- **Połączenie może uruchomić misję.** PX4 potrafi odmawiać uzbrojenia z powodu braku stacji naziemnej (`No connection to the GCS`). Jeśli misja na dronie cały czas próbuje uzbroić, heartbeat GUI to odblokowuje i dron startuje. Zaobserwowane z misją hydrolab w symulacji; QGC zadziała tak samo. Dlatego GUI pyta przed połączeniem.
- **GUI nie zastępuje RC.** Komendy idą przez radio i mogą nie dojść.
- **RTL, Land i Hold działają obok misji, nie razem z nią.** Misja na dronie nie wie o tych komendach i może je nadpisać: w hydrolabie Land w czasie zejścia do poboru próbki kończył się RTL na 30 m. Wyniki testów: `hydrolab26/uav_ui_plugin/README.md`.
- **GUI nie uzbraja drona, nie startuje misji i nie włącza Offboard.** Plugin też nie może zadeklarować takich komend MAVLink.
- **Nie ma uwierzytelniania.** Dostęp ogranicza nasłuch na localhost, sprawdzanie nagłówków `Host` i `Origin`, a na dronie nasłuch tylko na adresie tunelu.

## Mapa offline

Podkład **Ortofotomapa** (GUGiK, tylko Polska) ma cache na dysku. Przycisk **Pobierz obszar** zapisuje widoczny obszar przed wyjazdem w teren, limit to 3000 kafelków na raz. Esri World Imagery działa tylko online, bo Esri nie przewiduje tej warstwy do eksportu offline.

Poza zasięgiem ortofotomapy, np. dla domyślnej pozycji PX4 SITL w Zurychu, mapa jest czarna. Przełącz podkład na Esri.

## Testy

```bash
# backend
docker run --rm -v "$PWD:/app:ro" -w /app/backend python:3.12-slim bash -c \
  'pip install -q -r requirements.txt pytest && python -m pytest -q -p no:cacheprovider tests'

# sprawdzenie pluginu bez drona
docker compose run --rm -v "$PWD/plugins/example:/p:ro" app python -m uav_ui.validate /p

# bridge, w środowisku ROS 2
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python3 -m pytest bridge/uav_ui_bridge/test
```
