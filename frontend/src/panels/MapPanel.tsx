import maplibregl from "maplibre-gl";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { api, ApiError, describeResult, isSuccess, PrefetchJob } from "../api";
import { runPluginCommand } from "../commands";
import { useStore } from "../store";
import type { CommandResult, MapObject, MapSource, MapToolSpec, Plan, Plugin } from "../types";
import { ValueInput } from "../widgets";
import { hasPendingMapClick, subscribeMapClick, takeMapClick } from "./mapClick";

const DEFAULT_CENTER: [number, number] = [19.9172, 50.0598];
const DEFAULT_COLOR = "#ffffff";
const MAX_TRACK_POINTS = 2000;
const DEFAULT_ALTITUDE = 10;

interface DraftPoint {
  lat: number;
  lon: number;
  alt: number;
}

type DraftKind = "point" | "path";

interface Draft {
  kind: DraftKind;
  points: DraftPoint[];
}

const emptyCollection = (): GeoJSON.FeatureCollection => ({ type: "FeatureCollection", features: [] });

function safeColor(color: string | undefined, fallback: string) {
  return color && CSS.supports("color", color) ? color : fallback;
}

function line(coordinates: number[][], properties = {}): GeoJSON.Feature {
  return { type: "Feature", properties, geometry: { type: "LineString", coordinates } };
}

function setSource(map: maplibregl.Map, id: string, data: GeoJSON.FeatureCollection) {
  (map.getSource(id) as maplibregl.GeoJSONSource | undefined)?.setData(data);
}

function planToDraft(plan: Plan): Draft {
  const { geometry } = plan.plan;
  const positions =
    geometry.type === "Point"
      ? [geometry.coordinates as number[]]
      : (geometry.coordinates as number[][]);
  return {
    kind: geometry.type === "Point" ? "point" : "path",
    points: positions.map(([lon, lat, alt]) => ({ lat, lon, alt: alt ?? DEFAULT_ALTITUDE })),
  };
}

function draftToPlan(draft: Draft): Plan["plan"] {
  const positions = draft.points.map((p) => [p.lon, p.lat, p.alt]);
  return {
    type: "Feature",
    properties: {},
    geometry:
      draft.kind === "point"
        ? { type: "Point", coordinates: positions[0] }
        : { type: "LineString", coordinates: positions },
  };
}

function labelElement(text: string | undefined, color: string, extraClass = "") {
  const element = document.createElement("div");
  element.className = `map-marker ${extraClass}`;
  element.style.setProperty("--marker", color);
  const dot = document.createElement("span");
  dot.className = "dot";
  element.appendChild(dot);
  if (text) {
    const caption = document.createElement("span");
    caption.className = "caption";
    caption.textContent = text; // textContent: labels come from the drone and from plugins
    element.appendChild(caption);
  }
  const tip = document.createElement("div");
  tip.className = "map-tip";
  element.appendChild(tip);
  return element;
}

/** What hovering over a marker shows. Rebuilt on updates, so a moving object stays correct. */
function setMarkerTip(element: HTMLElement, title: string, lat: number, lon: number, details: string[]) {
  const tip = element.querySelector(".map-tip");
  if (!tip) return;
  const heading = document.createElement("strong");
  heading.textContent = title;
  const position = document.createElement("span");
  position.className = "position";
  position.textContent = `${lat.toFixed(6)}, ${lon.toFixed(6)}`;
  const lines = details.map((text) => {
    const line = document.createElement("div");
    line.textContent = text;
    return line;
  });
  tip.replaceChildren(heading, position, ...lines);
}

function objectTipDetails(object: MapObject, layerLabel: string | undefined) {
  const details: string[] = [];
  if (layerLabel) details.push(layerLabel);
  if (object.alt !== undefined) details.push(`Wysokość: ${object.alt.toFixed(1)} m`);
  if (object.approximate) details.push("Pozycja przybliżona: tu był dron w chwili wykrycia");
  if (object.t && object.alt !== undefined) {
    const time = new Date(object.t * 1000).toLocaleTimeString("pl-PL", { hour12: false });
    details.push(`Ostatnio: ${time}`);
  }
  return details;
}

export function MapPanel() {
  const container = useRef<HTMLDivElement>(null);
  const cursorRef = useRef<HTMLDivElement>(null);
  const mapRef = useRef<maplibregl.Map | null>(null);
  const objectMarkers = useRef(new Map<string, { marker: maplibregl.Marker; signature: string }>());
  const draftMarkers = useRef<maplibregl.Marker[]>([]);
  const vehicleMarker = useRef<maplibregl.Marker | null>(null);
  const homeMarker = useRef<maplibregl.Marker | null>(null);
  const track = useRef<number[][]>([]);
  const followed = useRef(false);

  const [sources, setSources] = useState<MapSource[]>([]);
  const [sourceId, setSourceId] = useState(() => localStorage.getItem("uav-ui-map-source") ?? "orto");
  const [ready, setReady] = useState(false);
  const [follow, setFollow] = useState(false);
  const [hiddenLayers, setHiddenLayers] = useState<Set<string>>(new Set());
  const [layersOpen, setLayersOpen] = useState(false);

  const [draft, setDraft] = useState<Draft | null>(null);
  const [planName, setPlanName] = useState("");
  const [plans, setPlans] = useState<Plan[]>([]);
  const [plansOpen, setPlansOpen] = useState(false);
  const [message, setMessage] = useState<{ ok: boolean; text: string } | null>(null);
  const [prefetch, setPrefetch] = useState<PrefetchJob | null>(null);
  const [waitingForPanelClick, setWaitingForPanelClick] = useState(false);
  const [toolArgs, setToolArgs] = useState<Record<string, Record<string, unknown>>>({});

  const telemetry = useStore((s) => s.telemetry);
  const bridgeCommands = useStore((s) => s.status.bridge?.info?.commands);
  const objects = useStore((s) => s.objects);
  const localObjects = useStore((s) => s.localObjects);
  const plugins = useStore((s) => s.plugins);
  const activePluginId = useStore((s) => s.activePlugin);
  const plugin: Plugin | undefined = plugins.find((p) => p.id === activePluginId);
  const scope = plugin?.id ?? "_global";

  const draftRef = useRef(draft);
  draftRef.current = draft;

  const allObjects = useMemo(
    () => [...Object.values(objects), ...Object.values(localObjects)],
    [objects, localObjects],
  );
  const layerStyles = useMemo(
    () => new Map((plugin?.map?.layers ?? []).map((layer) => [layer.id, layer])),
    [plugin],
  );
  const layerIds = useMemo(() => [...new Set(allObjects.map((o) => o.layer))], [allObjects]);

  const objectColor = useCallback(
    (object: MapObject) =>
      safeColor(object.color ?? layerStyles.get(object.layer)?.style?.color, DEFAULT_COLOR),
    [layerStyles],
  );

  useEffect(
    () =>
      subscribeMapClick(() => {
        setWaitingForPanelClick(hasPendingMapClick());
      }),
    [],
  );

  // ------------------------------------------------------------------ map set-up

  useEffect(() => {
    if (!container.current) return;
    const map = new maplibregl.Map({
      container: container.current,
      style: {
        version: 8,
        sources: {},
        layers: [{ id: "background", type: "background", paint: { "background-color": "#101412" } }],
      },
      center: DEFAULT_CENTER,
      zoom: 14,
      maxZoom: 21,
      attributionControl: { compact: true },
      dragRotate: false,
    });
    map.addControl(new maplibregl.NavigationControl({ showCompass: false }), "bottom-right");
    map.addControl(new maplibregl.ScaleControl({ unit: "metric" }), "bottom-left");

    map.on("load", () => {
      for (const id of ["objects", "plans", "draft", "track"]) {
        map.addSource(id, { type: "geojson", data: emptyCollection() });
      }
      map.addLayer({
        id: "track",
        type: "line",
        source: "track",
        paint: { "line-color": "#45c8f0", "line-width": 1.5, "line-opacity": 0.8 },
      });
      map.addLayer({
        id: "objects-lines",
        type: "line",
        source: "objects",
        paint: { "line-color": ["get", "color"], "line-width": 2.5 },
      });
      map.addLayer({
        id: "plans-lines",
        type: "line",
        source: "plans",
        paint: { "line-color": "#45c8f0", "line-width": 2, "line-dasharray": [2, 2] },
      });
      map.addLayer({
        id: "draft-lines",
        type: "line",
        source: "draft",
        paint: { "line-color": "#45c8f0", "line-width": 3 },
      });
      setReady(true);
    });

    map.on("click", (event) => {
      const { lat, lng: lon } = event.lngLat;
      const panelCallback = takeMapClick();
      if (panelCallback) {
        panelCallback(lat, lon);
        return;
      }
      const current = draftRef.current;
      if (!current) return;
      if (current.kind === "point") {
        const alt = current.points[0]?.alt ?? DEFAULT_ALTITUDE;
        setDraft({ kind: "point", points: [{ lat, lon, alt }] });
      } else {
        const alt = current.points.at(-1)?.alt ?? DEFAULT_ALTITUDE;
        setDraft({ kind: "path", points: [...current.points, { lat, lon, alt }] });
      }
    });

    map.on("mousemove", (event) => {
      if (cursorRef.current) {
        const { lat, lng } = event.lngLat;
        cursorRef.current.textContent = `${lat.toFixed(6)}, ${lng.toFixed(6)}`;
      }
    });
    map.on("mouseout", () => {
      if (cursorRef.current) cursorRef.current.textContent = "";
    });

    const resize = new ResizeObserver(() => map.resize());
    resize.observe(container.current);
    mapRef.current = map;
    const markers = objectMarkers.current;

    return () => {
      resize.disconnect();
      markers.clear();
      vehicleMarker.current = null;
      homeMarker.current = null;
      draftMarkers.current = [];
      setReady(false);
      map.remove();
      mapRef.current = null;
    };
  }, []);

  useEffect(() => {
    api
      .mapSources()
      .then((result) => setSources(result.sources))
      .catch(() => setMessage({ ok: false, text: "Nie udało się pobrać listy map." }));
  }, []);

  useEffect(() => {
    const map = mapRef.current;
    const source = sources.find((s) => s.id === sourceId) ?? sources[0];
    if (!map || !ready || !source) return;
    if (map.getLayer("base")) map.removeLayer("base");
    if (map.getSource("base")) map.removeSource("base");
    map.addSource("base", {
      type: "raster",
      tiles: [source.tiles.startsWith("/") ? location.origin + source.tiles : source.tiles],
      tileSize: 256,
      maxzoom: source.max_zoom,
      attribution: source.attribution,
    });
    map.addLayer({ id: "base", type: "raster", source: "base" }, "track");
    localStorage.setItem("uav-ui-map-source", source.id);
  }, [ready, sources, sourceId]);

  // ------------------------------------------------------------------ vehicle

  const lat = telemetry?.lat ?? null;
  const lon = telemetry?.lon ?? null;
  const heading = telemetry?.heading ?? 0;
  const online = telemetry?.online ?? false;
  const home = telemetry?.home ?? null;
  const armed = telemetry?.armed ?? false;

  useEffect(() => {
    const map = mapRef.current;
    if (!map || !ready || lat === null || lon === null) return;

    if (!vehicleMarker.current) {
      const element = document.createElement("div");
      element.className = "vehicle-marker";
      element.title = "Dron";
      vehicleMarker.current = new maplibregl.Marker({
        element,
        rotationAlignment: "map",
      })
        .setLngLat([lon, lat])
        .addTo(map);
    }
    vehicleMarker.current.setLngLat([lon, lat]).setRotation(heading);
    vehicleMarker.current.getElement().classList.toggle("stale", !online);

    if (armed) {
      const previous = track.current.at(-1);
      if (!previous || previous[0] !== lon || previous[1] !== lat) {
        track.current = [...track.current.slice(-MAX_TRACK_POINTS), [lon, lat]];
        setSource(map, "track", {
          type: "FeatureCollection",
          features: track.current.length > 1 ? [line(track.current)] : [],
        });
      }
    }

    if (!followed.current) {
      followed.current = true;
      map.jumpTo({ center: [lon, lat], zoom: Math.max(map.getZoom(), 17) });
    } else if (follow) {
      map.easeTo({ center: [lon, lat], duration: 200 });
    }
  }, [ready, lat, lon, heading, online, armed, follow]);

  useEffect(() => {
    const map = mapRef.current;
    if (!map || !ready || !home) return;
    if (!homeMarker.current) {
      homeMarker.current = new maplibregl.Marker({
        element: labelElement("Start", "#ffffff", "home-marker"),
      })
        .setLngLat([home.lon, home.lat])
        .addTo(map);
    } else {
      homeMarker.current.setLngLat([home.lon, home.lat]);
    }
    setMarkerTip(homeMarker.current.getElement(), "Miejsce startu", home.lat, home.lon, []);
  }, [ready, home]);

  // ------------------------------------------------------------------ objects from plugins

  useEffect(() => {
    const map = mapRef.current;
    if (!map || !ready) return;
    const visible = allObjects.filter((object) => !hiddenLayers.has(object.layer));

    setSource(map, "objects", {
      type: "FeatureCollection",
      features: visible
        .filter((object) => object.kind === "path" && (object.points?.length ?? 0) > 1)
        .map((object) =>
          line(object.points!.map((p) => [p[1], p[0]]), { color: objectColor(object) }),
        ),
    });

    const wanted = new Set<string>();
    for (const object of visible) {
      if (object.kind !== "marker" || object.lat === undefined || object.lon === undefined) continue;
      wanted.add(object.id);
      const color = objectColor(object);
      const label = object.approximate
        ? `około: ${object.label ?? object.key}`
        : (object.label ?? (object.key === object.layer ? undefined : object.key));
      const signature = `${color}|${label}`;
      const current = objectMarkers.current.get(object.id);
      let marker = current?.marker;
      if (!current || current.signature !== signature) {
        current?.marker.remove();
        const element = labelElement(label, color, object.approximate ? "approximate" : "");
        marker = new maplibregl.Marker({ element, anchor: "left", offset: [-6, 0] })
          .setLngLat([object.lon, object.lat])
          .addTo(map);
        objectMarkers.current.set(object.id, { marker, signature });
      }
      marker!.setLngLat([object.lon, object.lat]);
      const layerLabel = layerStyles.get(object.layer)?.label;
      setMarkerTip(
        marker!.getElement(),
        object.label ?? object.key,
        object.lat,
        object.lon,
        objectTipDetails(object, layerLabel),
      );
    }
    for (const [id, entry] of objectMarkers.current) {
      if (!wanted.has(id)) {
        entry.marker.remove();
        objectMarkers.current.delete(id);
      }
    }
  }, [ready, allObjects, hiddenLayers, objectColor, layerStyles]);

  // ------------------------------------------------------------------ draft and plans

  useEffect(() => {
    const map = mapRef.current;
    if (!map || !ready) return;
    draftMarkers.current.forEach((marker) => marker.remove());
    draftMarkers.current = [];
    const points = draft?.points ?? [];
    setSource(map, "draft", {
      type: "FeatureCollection",
      features: points.length > 1 ? [line(points.map((p) => [p.lon, p.lat]))] : [],
    });
    points.forEach((point, index) => {
      const element = document.createElement("div");
      element.className = "draft-marker";
      element.textContent = draft?.kind === "path" ? String(index + 1) : "";
      element.addEventListener("click", (event) => event.stopPropagation());
      const marker = new maplibregl.Marker({ element, draggable: true })
        .setLngLat([point.lon, point.lat])
        .addTo(map);
      marker.on("dragend", () => {
        const position = marker.getLngLat();
        setDraft((current) =>
          current
            ? {
                ...current,
                points: current.points.map((p, i) =>
                  i === index ? { ...p, lat: position.lat, lon: position.lng } : p,
                ),
              }
            : current,
        );
      });
      draftMarkers.current.push(marker);
    });
  }, [ready, draft]);

  const refreshPlans = useCallback(() => {
    api
      .plans(scope)
      .then(setPlans)
      .catch(() => setPlans([]));
  }, [scope]);

  useEffect(refreshPlans, [refreshPlans]);

  useEffect(() => {
    const map = mapRef.current;
    if (!map || !ready) return;
    setSource(map, "plans", {
      type: "FeatureCollection",
      features: plans
        .filter((plan) => plan.plan.geometry.type === "LineString")
        .map((plan) => line((plan.plan.geometry.coordinates as number[][]).map((c) => c.slice(0, 2)))),
    });
  }, [ready, plans]);

  const startDraft = (kind: DraftKind) => {
    setMessage(null);
    setDraft({ kind, points: [] });
  };

  const toolsFor = (kind: DraftKind): MapToolSpec[] =>
    plugin?.ui?.map_tools?.filter((tool) => tool.kind === kind && tool.command) ?? [];

  const savePlan = async () => {
    if (!draft || !draft.points.length || !planName.trim()) return;
    try {
      await api.savePlan(scope, planName.trim(), draftToPlan(draft));
      setMessage({ ok: true, text: `Zapisano „${planName.trim()}”.` });
      refreshPlans();
    } catch (error) {
      if (!(error instanceof ApiError)) throw error;
      setMessage({ ok: false, text: error.message });
    }
  };

  const sendDraft = async (tool: MapToolSpec) => {
    if (!draft || !plugin || !tool.command) return;
    const specs = plugin.commands?.[tool.command]?.args ?? {};
    const position =
      draft.kind === "point"
        ? { lat: draft.points[0].lat, lon: draft.points[0].lon, alt: draft.points[0].alt }
        : { points: draft.points };
    const args: Record<string, unknown> = { ...(toolArgs[tool.id] ?? {}) };
    for (const [name, value] of Object.entries(position)) {
      if (name in specs) args[name] = value;
    }
    const result: CommandResult = await runPluginCommand(plugin, tool.command, args);
    if (result.status === "cancelled") return;
    setMessage({ ok: isSuccess(result), text: describeResult(result) });
  };

  const loadFromLayer = (layer: string) => {
    const route = allObjects.find((o) => o.layer === layer && o.kind === "path");
    if (!route?.points?.length) {
      setMessage({ ok: false, text: "Na dronie nie ma jeszcze tej trasy." });
      return;
    }
    setDraft({
      kind: "path",
      points: route.points.map(([lat, lon, alt]) => ({ lat, lon, alt })),
    });
  };

  const deletePlan = async (name: string) => {
    await api.deletePlan(scope, name);
    refreshPlans();
  };

  const downloadArea = async () => {
    const map = mapRef.current;
    if (!map) return;
    const bounds = map.getBounds();
    const zoom = Math.floor(map.getZoom());
    try {
      let job = await api.prefetch({
        source: sourceId,
        west: bounds.getWest(),
        south: bounds.getSouth(),
        east: bounds.getEast(),
        north: bounds.getNorth(),
        min_zoom: Math.max(zoom - 2, 0),
        max_zoom: Math.min(zoom + 2, 19),
      });
      setPrefetch(job);
      while (!job.finished) {
        await new Promise((resolve) => setTimeout(resolve, 700));
        job = await api.prefetchStatus(job.id);
        setPrefetch(job);
      }
      setMessage({
        ok: job.failed === 0,
        text: `Pobrano obszar: ${job.total - job.failed} z ${job.total} kafelków.`,
      });
    } catch (error) {
      if (!(error instanceof ApiError)) throw error;
      setMessage({ ok: false, text: error.message });
    } finally {
      setPrefetch(null);
    }
  };

  const currentSource = sources.find((s) => s.id === sourceId);
  const draftTools = draft ? toolsFor(draft.kind) : [];
  const positionArgs = ["lat", "lon", "alt", "points"];
  const showAltitude =
    draft?.kind === "path" ||
    draftTools.length === 0 ||
    draftTools.some((tool) => "alt" in (plugin?.commands?.[tool.command!]?.args ?? {}));
  const layerToLoad = draftTools.find((tool) => tool.load_layer)?.load_layer;

  return (
    <div className="map-panel">
      <div ref={container} className="map-canvas" />
      <div ref={cursorRef} className="map-cursor" aria-hidden="true" />

      <div className="map-toolbar">
        <div className="tool-group">
          <select
            aria-label="Podkład mapy"
            value={sourceId}
            onChange={(event) => setSourceId(event.target.value)}
          >
            {sources.map((source) => (
              <option key={source.id} value={source.id}>
                {source.label}
              </option>
            ))}
          </select>
          {currentSource?.offline && (
            <button
              disabled={prefetch !== null}
              title="Zapisuje widoczny obszar do pracy bez internetu"
              onClick={() => void downloadArea()}
            >
              {prefetch ? `Pobieranie ${prefetch.done} z ${prefetch.total}` : "Pobierz obszar"}
            </button>
          )}
        </div>
        <div className="tool-group">
          <button
            className={draft?.kind === "point" ? "active" : ""}
            aria-pressed={draft?.kind === "point"}
            onClick={() => (draft?.kind === "point" ? setDraft(null) : startDraft("point"))}
          >
            Punkt
          </button>
          <button
            className={draft?.kind === "path" ? "active" : ""}
            aria-pressed={draft?.kind === "path"}
            onClick={() => (draft?.kind === "path" ? setDraft(null) : startDraft("path"))}
          >
            Ścieżka
          </button>
          <button
            className={plansOpen ? "active" : ""}
            aria-pressed={plansOpen}
            onClick={() => {
              setPlansOpen(!plansOpen);
              setLayersOpen(false);
            }}
          >
            Plany ({plans.length})
          </button>
        </div>
        <div className="tool-group">
          <button
            className={layersOpen ? "active" : ""}
            aria-pressed={layersOpen}
            onClick={() => {
              setLayersOpen(!layersOpen);
              setPlansOpen(false);
            }}
          >
            Warstwy
          </button>
          <button className={follow ? "active" : ""} aria-pressed={follow} onClick={() => setFollow(!follow)}>
            Śledź drona
          </button>
        </div>
      </div>

      {waitingForPanelClick && <div className="map-hint">Kliknij punkt na mapie dla panelu pluginu.</div>}

      {draft && (
        <div className="map-card draft-card">
          <div className="card-title">
            {draft.kind === "point" ? "Punkt" : "Ścieżka"}
            <button className="small" onClick={() => setDraft(null)}>
              Zamknij
            </button>
          </div>
          {draft.points.length === 0 && (
            <div className="muted small-text">
              {draft.kind === "point"
                ? "Kliknij na mapie, żeby wybrać punkt."
                : "Klikaj na mapie, żeby dodawać kolejne punkty. Punkty można przeciągać."}
            </div>
          )}
          {layerToLoad && (
            <button onClick={() => loadFromLayer(layerToLoad)}>Wczytaj trasę z drona</button>
          )}
          <div className="draft-points">
            {draft.points.map((point, index) => (
              <div className="draft-point" key={index}>
                <span className="mono small-text">
                  {index + 1}. {point.lat.toFixed(6)}, {point.lon.toFixed(6)}
                </span>
                {showAltitude && (
                  <label>
                    <span className="sr-only">Wysokość punktu {index + 1}</span>
                    <input
                      type="number"
                      value={point.alt}
                      step="any"
                      onChange={(event) =>
                        setDraft({
                          ...draft,
                          points: draft.points.map((p, i) =>
                            i === index ? { ...p, alt: Number(event.target.value) } : p,
                          ),
                        })
                      }
                    />
                    m
                  </label>
                )}
                <button
                  className="small"
                  aria-label={`Usuń punkt ${index + 1}`}
                  onClick={() =>
                    setDraft({ ...draft, points: draft.points.filter((_, i) => i !== index) })
                  }
                >
                  ✕
                </button>
              </div>
            ))}
          </div>
          {draft.points.length > 0 && (
            <>
              <div className="card-row">
                <input
                  aria-label="Nazwa planu"
                  placeholder="Nazwa"
                  value={planName}
                  onChange={(event) => setPlanName(event.target.value)}
                />
                <button disabled={!planName.trim()} onClick={() => void savePlan()}>
                  Zapisz {plugin ? `w ${plugin.name}` : "lokalnie"}
                </button>
              </div>
              {draftTools.map((tool) => {
                const command = plugin?.commands?.[tool.command!];
                const availability = bridgeCommands?.[tool.command!];
                const extras = Object.entries(command?.args ?? {}).filter(
                  ([name]) => !positionArgs.includes(name),
                );
                const values = toolArgs[tool.id] ?? {};
                const incomplete = extras.some(
                  ([name]) => values[name] === undefined || values[name] === "",
                );
                return (
                  <div className="tool-action" key={tool.id}>
                    {extras.map(([name, spec]) => (
                      <ValueInput
                        key={name}
                        name={name}
                        spec={{ ...spec, values: spec.values }}
                        value={values[name] ?? ""}
                        onChange={(value) =>
                          setToolArgs((current) => ({
                            ...current,
                            [tool.id]: { ...current[tool.id], [name]: value },
                          }))
                        }
                      />
                    ))}
                    <button
                      className="primary"
                      disabled={availability?.available === false || incomplete}
                      onClick={() => void sendDraft(tool)}
                    >
                      {tool.label}
                    </button>
                    {availability?.available === false && (
                      <div className="result bad">
                        Dron nie obsługuje tej komendy: {availability.error}.
                      </div>
                    )}
                  </div>
                );
              })}
              {draftTools.length === 0 && (
                <div className="muted small-text">
                  {plugin
                    ? "Ten plugin nie ma komendy dla tego narzędzia."
                    : "Wysyłanie do drona wymaga pluginu z odpowiednią komendą."}
                </div>
              )}
            </>
          )}
        </div>
      )}

      {plansOpen && !draft && (
        <div className="map-card">
          <div className="card-title">Plany: {plugin?.name ?? "bez pluginu"}</div>
          {plans.length === 0 && (
            <div className="muted small-text">Nie ma planów. Użyj narzędzia Punkt albo Ścieżka i zapisz wynik.</div>
          )}
          {plans.map((plan) => (
            <div className="card-row" key={plan.name}>
              <span className="grow">{plan.name}</span>
              <button
                className="small"
                onClick={() => {
                  setDraft(planToDraft(plan));
                  setPlanName(plan.name);
                  const first = planToDraft(plan).points[0];
                  mapRef.current?.easeTo({ center: [first.lon, first.lat] });
                }}
              >
                Otwórz
              </button>
              <button className="small" onClick={() => void deletePlan(plan.name)}>
                Usuń
              </button>
            </div>
          ))}
        </div>
      )}

      {layersOpen && !draft && (
        <div className="map-card">
          <div className="card-title">Warstwy</div>
          {layerIds.length === 0 && (
            <div className="muted small-text">Warstwy pojawią się po połączeniu z dronem, który ma plugin.</div>
          )}
          {layerIds.map((id) => (
            <label className="field check" key={id}>
              <input
                type="checkbox"
                checked={!hiddenLayers.has(id)}
                onChange={(event) => {
                  const next = new Set(hiddenLayers);
                  if (event.target.checked) next.delete(id);
                  else next.add(id);
                  setHiddenLayers(next);
                }}
              />
              <i
                className="swatch"
                style={{ background: safeColor(layerStyles.get(id)?.style?.color, DEFAULT_COLOR) }}
              />
              {layerStyles.get(id)?.label ?? id}
            </label>
          ))}
        </div>
      )}

      {message && (
        <button
          className={`map-message result ${message.ok ? "ok" : "bad"}`}
          title="Kliknij, żeby zamknąć"
          onClick={() => setMessage(null)}
        >
          {message.text}
        </button>
      )}
    </div>
  );
}
