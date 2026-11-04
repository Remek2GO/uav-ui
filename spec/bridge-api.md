# API bridge'a

HTTP/1.1 z JSON-em, między backendem GUI a `uav_ui_bridge`. Klientem jest wyłącznie backend, a żądania z nagłówkiem `Origin` (przeglądarka) są odrzucane kodem 403. Inne projekty, także bez ROS-a, mogą wystawić to samo API.

## `GET /info`

```json
{
  "api": 1,
  "plugin": {"id": "hydrolab", "version": "0.1.0", "sha256": "..."},
  "commands": {"say": {"available": true}, "route": {"available": false, "error": "..."}},
  "unavailable": {"commands": {}, "state": {}, "layers": {}, "images": {}}
}
```

GUI porównuje `sha256` z wgranym plikiem i ostrzega przy różnicy.

## `GET /poll?state=N&events=N&map=N&actions=N`

Zwraca tylko to, co zmieniło się po numerze `N`. Numer pochodzi z pola `seq` poprzedniej odpowiedzi. Gdy `seq` maleje, bridge się zrestartował i klient pyta o wszystko od zera. Backend odpytuje raz na sekundę.

```json
{
  "seq": 120,
  "now": 1793823000.1,
  "state": {"temperature": {"v": 18.5, "t": 1793823000.0}},
  "events": [{"seq": 118, "t": 1793823000.0, "level": "info", "source": "node", "text": "..."}],
  "objects": [{"id": "pools/red", "layer": "pools", "key": "red", "kind": "marker", "lat": 54.05, "lon": 22.14, "label": "red", "color": "red", "seq": 3, "t": 0}],
  "actions": {"abc": {"command": "fibonacci", "status": "running", "feedback": {}, "result": null}}
}
```

Obiekt może mieć `kind: "path"` z `points: [[lat, lon, alt], ...]` oraz `approximate: true`.

## `POST /commands/{name}`

```json
{"id": "unikalny-id", "args": {"ml": 500}}
```

`id` jest wymagane. **Powtórzenie z tym samym `id` nie wykonuje komendy ponownie**, tylko zwraca poprzedni wynik z `"duplicate": true`. Dzięki temu po utracie odpowiedzi można bezpiecznie ponowić żądanie.

| `status` | Znaczenie | Pola |
|---|---|---|
| `ok` | Wykonano | `subscribers` (topic) lub `result` (serwis) |
| `accepted` | Akcja wysłana, dalszy stan w `/poll` | `action_id` |
| `rejected` | Nie wykonano: nieznana komenda, zły argument, typ niedostępny | `error` |
| `error` | Nie udało się wykonać | `error` |

## `POST /actions/{id}/cancel`

Prosi serwer akcji o anulowanie. Wynik pojawi się w `/poll`.

## `GET /image/{name}`

JPEG. `204` oznacza, że obrazu jeszcze nie ma, a `304` odpowiada na `If-None-Match` przy braku nowej klatki. Pierwsze żądanie zaczyna subskrypcję, która kończy się po 10 s bez żądań.
