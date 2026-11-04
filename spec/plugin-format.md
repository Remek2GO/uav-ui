# Format pluginu

Plugin to katalog z plikiem `plugin.yaml`. Trzymaj go w repozytorium projektu, którym steruje, bo komendy zmieniają się razem z jego topikami. Ten sam plik czytają dwie strony:

- **GUI:** przyciski, formularze, wykresy, warstwy mapy.
- **`uav_ui_bridge` na dronie:** mapowanie na ROS. Bridge wykonuje wyłącznie komendy z tego pliku.

Pełny schemat: [plugin.schema.json](plugin.schema.json). Działający przykład wszystkich pól: [plugins/example](../plugins/example/plugin.yaml). Sprawdzenie bez drona: `python -m uav_ui.validate <katalog>`.

> **Uwaga na YAML:** nawiasy kwadratowe w wartości wewnątrz `{...}` psują plik, np. `{label: Czas [s]}`. Użyj `unit: s`.

## Główne pola

| Pole | Znaczenie |
|---|---|
| `api` | Zawsze `1` |
| `id` | Małe litery, cyfry, `-`, `_` |
| `name`, `version`, `description` | Opis |
| `config` | Ustawienia edytowane w GUI, zapisane na laptopie |
| `commands` | Co można wysłać |
| `state` | Wartości do wyświetlania |
| `events` | Co trafia do panelu zdarzeń |
| `images` | Podgląd obrazów |
| `map` | Warstwy mapy |
| `ui` | Panele i narzędzia mapy |

## Komendy

Dowolny typ zbudowany w workspace drona, także własne wiadomości. Nazwa komendy nie musi się zgadzać z topikiem.

```yaml
commands:
  pump:
    label: Pompa
    description: Pokazywane w podpowiedzi
    confirm: true            # albo tekst pytania
    args:
      ml: {type: number, label: Objętość, unit: ml, min: 1, max: 5000, default: $config.volume}
    ros:
      topic: /pump/dispense
      type: std_msgs/Float64
      fields: {data: $ml}
      qos: {reliability: reliable, durability: volatile, depth: 10}
```

**Rodzaje:** dokładnie jedno z:

| Pole | Wynik w GUI |
|---|---|
| `ros: {topic, type, fields, qos, oneshot}` | Liczba odbiorców |
| `ros: {service, type, fields}` | Odpowiedź serwisu |
| `ros: {action, type, fields}` | Stan, feedback, wynik, przycisk anulowania |
| `mavlink: {command, params}` | Wynik `COMMAND_ACK` |

Komend MAVLink, które uzbrajają, startują, zmieniają tryb lub restartują autopilota, nie da się zadeklarować. Plugin z taką komendą zostanie odrzucony.

**Typy argumentów:** `number`, `integer`, `boolean`, `enum` (z `values`), `string`, `points`. Nadmiarowe argumenty i wartości spoza zakresu są odrzucane na dronie.

**Pola wiadomości** (`fields`):

| Zapis | Znaczenie |
|---|---|
| `pose.position.x: $x` | Zagnieżdżone pole, wartość z argumentu |
| `header.stamp: $now` | Bieżący czas |
| `header.frame_id: map` | Stała wartość |
| `poses: {$each: $points, item: {pose.position.latitude: $lat}}` | Lista: jeden element na punkt |

**`oneshot: true`:** publisher istnieje tylko na czas komendy. Użyj go z `transient_local`, gdy węzeł uruchomiony później nie może dostać starej wartości, np. celu lotu.

## Stan

```yaml
state:
  temperature:
    topic: /sensors
    type: std_msgs/Float32MultiArray
    field: "data[1]"      # domyślnie data
    label: Temperatura
    unit: °C
    precision: 2
```

Przez radio idzie najnowsza próbka raz na sekundę i tylko po zmianie, niezależnie od częstotliwości topicu. Domyślne QoS (`best_effort`, `volatile`) pasuje do każdego wydawcy. Dla wartości publikowanych raz ustaw `qos: {reliability: reliable, durability: transient_local, depth: 1}`.

## Zdarzenia

```yaml
events:
  rosout: {min_level: info, exclude_nodes: [micro_ros_agent], max_per_s: 5}
  topics:
    - {topic: /found, type: my_msgs/Box, level: warn, source: wykrywanie, text: "Wykryto: {color}"}
```

Kody kolorów ANSI są usuwane, a identyczny tekst z tego samego źródła przechodzi najwyżej raz na 5 s. Dla zdarzeń z `topics` zmienisz to polem `min_interval_s`: wiadomość płynąca z każdej klatki kamery nie powinna zalewać logu.

## Mapa

```yaml
map:
  origin:   {topic: /fmu/out/vehicle_local_position_v1, type: px4_msgs/msg/VehicleLocalPosition, lat: ref_lat, lon: ref_lon, alt: ref_alt}
  vehicle:  {topic: /fmu/out/vehicle_global_position, type: px4_msgs/msg/VehicleGlobalPosition, lat: lat, lon: lon, alt: alt}
  layers:
    - id: goals
      label: Cele
      style: {color: "#3fb950"}
      source: {topic: /goals, type: sensor_msgs/NavSatFix, key: header.frame_id, label: header.frame_id}
```

| Źródło warstwy | Uwagi |
|---|---|
| `sensor_msgs/NavSatFix`, `geographic_msgs/GeoPointStamped`, `GeoPoseStamped` | Punkt, bez konfiguracji |
| `geographic_msgs/GeoPath` | Ścieżka, bez konfiguracji |
| `geometry_msgs/PoseStamped`, `PointStamped` | Punkt. Wymaga `frame: ned` lub `enu` oraz `map.origin` |
| `geometry_msgs/PoseArray`, `nav_msgs/Path` | Ścieżka. Wymaga `frame` oraz `map.origin` |
| Dowolna wiadomość z `lat`, `lon` (i `alt`) | Wskazujesz pola |
| Dowolna wiadomość z `at: vehicle` | Obiekt w pozycji drona z chwili odebrania, oznaczony jako przybliżony. Wymaga `map.vehicle` |
| `file: $zmienna`, `path: a.b.c`, `lat`, `lon` | Stałe punkty z pliku YAML na dronie |

Pola wspólne: `key` (wiele obiektów na warstwie, np. `color`), `label`, `color`, `min_interval_s`, `qos`.

`$zmienna` w `file` to wartość z parametru `plugin_vars` bridge'a, np. `navigation_file=/ścieżka`.

## Obrazy

```yaml
images:
  found: {topic: /found_image, type: sensor_msgs/Image, max_hz: 0.5, max_width: 480, quality: 50}
```

Bridge subskrybuje topic tylko przy otwartym panelu i wysyła JPEG. Limity to najwyżej 2 Hz i 1280 px.

## Interfejs

```yaml
ui:
  panels:
    - id: mission
      title: Misja
      widgets:
        - {type: button, command: go, label: Czerwony, args: {color: red}}
        - {type: form, command: pump}
        - {type: value, state: temperature}
        - {type: status, state: active, on_text: Pracuje, off_text: Stop}
        - {type: gauge, state: temperature, min: 0, max: 40}
        - {type: table, states: [temperature, ph]}
        - {type: chart, states: [temperature, ph], window_s: 300}
        - {type: image, image: found}
        - {type: events}
    - {id: custom, title: Własny, type: custom, entry: panel.html}
  map_tools:
    - {id: goal, kind: point, label: Punkt, command: send_goal}
    - {id: route, kind: path, label: Ścieżka, command: send_route}
```

- **`button`** wysyła od razu, z ustalonymi `args`. **`form`** pokazuje pola.
- **`map_tools`:** komenda dostaje `lat`, `lon`, `alt` (`kind: point`) albo `points` (`kind: path`). Zapis lokalny działa też bez komendy.
- **Panel własny:** patrz [custom-panel-api.md](custom-panel-api.md).

## Na dronie

Dodaj węzeł do launcha i pakiet do obrazu. Wzorzec: `hydrolab26/drone_core/launch/rpi_hydrolab.launch.py`.

| Parametr | Znaczenie |
|---|---|
| `plugin_file` | Ścieżka do `plugin.yaml` |
| `bind_address` | Adres tunelu, np. `10.5.0.2`. Bind jest ponawiany, aż tunel wstanie |
| `port` | Domyślnie `8765` |
| `plugin_vars` | Zmienne dla `file:`, np. `a=1;b=2` |

Typ niedostępny na dronie wyłącza tylko korzystającą z niego komendę lub warstwę, a GUI to pokazuje.
