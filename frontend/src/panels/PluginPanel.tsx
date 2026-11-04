import { useEffect, useRef } from "react";
import { describeResult } from "../api";
import { runPluginCommand } from "../commands";
import { getStore, setStore, useStore } from "../store";
import type { MapObject, PanelSpec, Plugin } from "../types";
import { Widget } from "../widgets";
import { requestMapClick } from "./mapClick";

export function PluginPanel({ pluginId, panelId }: { pluginId: string; panelId: string }) {
  const plugin = useStore((s) => s.plugins.find((p) => p.id === pluginId));
  const panel = plugin?.ui?.panels?.find((p) => p.id === panelId);

  if (!plugin || !panel) return <div className="empty">Tego panelu już nie ma w pluginie.</div>;
  if (panel.type === "custom") return <CustomPanel plugin={plugin} panel={panel} />;

  return (
    <div className="panel">
      {(panel.widgets ?? []).map((widget, index) => (
        <Widget key={index} plugin={plugin} widget={widget} />
      ))}
      {!panel.widgets?.length && <div className="empty">Ten panel nie ma widgetów.</div>}
    </div>
  );
}

function CustomPanel({ plugin, panel }: { plugin: Plugin; panel: PanelSpec }) {
  const frame = useRef<HTMLIFrameElement>(null);
  const state = useStore((s) => s.state);

  const post = (message: unknown) => frame.current?.contentWindow?.postMessage(message, "*");

  useEffect(() => {
    post({ type: "state", values: state });
  }, [state]);

  useEffect(() => {
    const onMessage = async (event: MessageEvent) => {
      // The sandbox gives the panel an opaque origin, so the window identity is what proves who it is.
      if (event.source !== frame.current?.contentWindow) return;
      const message = event.data;
      if (typeof message !== "object" || message === null) return;

      if (message.type === "ready") {
        post({ type: "init", plugin: plugin.id, config: plugin.config_values });
        post({ type: "state", values: getStore().state });
      } else if (message.type === "command") {
        const args = typeof message.args === "object" && message.args ? message.args : {};
        const result = await runPluginCommand(plugin, String(message.name), args);
        post({
          type: "command_result",
          requestId: message.requestId,
          result,
          text: describeResult(result),
        });
      } else if (message.type === "request_map_click") {
        requestMapClick((lat, lon) => post({ type: "map_click", lat, lon }));
      } else if (message.type === "set_layer") {
        setPanelLayer(plugin.id, String(message.id), message.objects);
      }
    };
    const listener = (event: MessageEvent) => void onMessage(event);
    window.addEventListener("message", listener);
    return () => window.removeEventListener("message", listener);
  }, [plugin]);

  return (
    <iframe
      ref={frame}
      className="custom-panel"
      title={panel.title}
      sandbox="allow-scripts"
      src={`/plugin-assets/${plugin.id}/${panel.entry}`}
    />
  );
}

const MAX_PANEL_OBJECTS = 1000;

/** A custom panel can draw on the map, but only under its own plugin's prefix, and with checked data. */
function setPanelLayer(pluginId: string, layerId: string, objects: unknown) {
  const prefix = `panel:${pluginId}:${layerId}`;
  const kept = Object.fromEntries(
    Object.entries(getStore().localObjects).filter(([id]) => !id.startsWith(`${prefix}/`)),
  );
  if (Array.isArray(objects)) {
    for (const raw of objects.slice(0, MAX_PANEL_OBJECTS)) {
      const object = sanitizeObject(raw, prefix);
      if (object) kept[object.id] = object;
    }
  }
  setStore({ localObjects: kept });
}

function validCoordinate(lat: unknown, lon: unknown) {
  return (
    typeof lat === "number" &&
    typeof lon === "number" &&
    Math.abs(lat) <= 90 &&
    Math.abs(lon) <= 180
  );
}

function sanitizeObject(raw: unknown, prefix: string): MapObject | null {
  if (typeof raw !== "object" || raw === null) return null;
  const item = raw as Record<string, unknown>;
  const key = String(item.key ?? "");
  const base = {
    id: `${prefix}/${key}`,
    layer: prefix,
    key,
    label: typeof item.label === "string" ? item.label : undefined,
    color: typeof item.color === "string" ? item.color : undefined,
  };
  if (item.kind === "path" && Array.isArray(item.points)) {
    const points = item.points.filter(
      (point): point is [number, number, number] =>
        Array.isArray(point) && validCoordinate(point[0], point[1]),
    );
    return { ...base, kind: "path", points };
  }
  if (validCoordinate(item.lat, item.lon)) {
    return { ...base, kind: "marker", lat: item.lat as number, lon: item.lon as number };
  }
  return null;
}
