# API panelu własnego

Do rzeczy, których nie da się opisać widgetami. Panel to jeden plik HTML w katalogu pluginu:

```yaml
ui:
  panels:
    - {id: scan, title: Skan obszaru, type: custom, entry: panel.html}
```

Przykład: [plugins/example/panel.html](../plugins/example/panel.html).

## Ograniczenia

Panel działa w `<iframe sandbox="allow-scripts">`, z nagłówkiem CSP bez dostępu do sieci.

- **Wszystko w jednym pliku:** skrypty i style inline, obrazy jako `data:`.
- **Brak dostępu** do sieci, `localStorage`, cookies i strony GUI.
- **Jedyny kanał** to `postMessage` do okna nadrzędnego.
- **Komendy** są tylko te z `plugin.yaml` i przechodzą przez to samo potwierdzenie co przyciski. Panel nie może go pominąć.

## Z panelu do GUI

Wysyłaj przez `parent.postMessage(wiadomość, '*')`.

| `type` | Pola | Działanie |
|---|---|---|
| `ready` | | Panel gotowy, GUI odsyła `init` i `state` |
| `command` | `requestId`, `name`, `args` | Uruchamia komendę pluginu |
| `request_map_click` | | Następne kliknięcie na mapie trafi do panelu |
| `set_layer` | `id`, `objects` | Rysuje na mapie. Pusta lista czyści warstwę. Limit: 1000 obiektów |

Obiekt warstwy to jedno z:

```js
{key: 'a', kind: 'marker', lat: 54.05, lon: 22.14, label: 'A', color: '#ff00ff'}
{key: 'b', kind: 'path', points: [[54.05, 22.14, 10], [54.06, 22.15, 10]], color: 'red'}
```

## Z GUI do panelu

Odbieraj przez `window.addEventListener('message', ...)` i sprawdzaj `event.source === parent`.

| `type` | Pola | Kiedy |
|---|---|---|
| `init` | `plugin`, `config` | Po `ready` |
| `state` | `values`: `{klucz: {v, t}}` | Po każdej zmianie stanu |
| `command_result` | `requestId`, `result`, `text` | Po komendzie. `result.status` może być `cancelled`, gdy operator odmówił |
| `map_click` | `lat`, `lon` | Po kliknięciu na mapie |
