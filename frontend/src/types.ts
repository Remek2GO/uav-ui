export type Level = "debug" | "info" | "warn" | "error" | "fatal";

export interface EventItem {
  id: string;
  t: number;
  level: Level;
  source: string;
  text: string;
}

export interface Telemetry {
  online: boolean;
  autopilot: string | null;
  armed: boolean;
  mode: string | null;
  lat: number | null;
  lon: number | null;
  alt: number | null;
  rel_alt: number | null;
  heading: number | null;
  groundspeed: number | null;
  climb: number | null;
  battery_pct: number | null;
  battery_v: number | null;
  gps_fix: number | null;
  satellites: number | null;
  home: { lat: number; lon: number } | null;
  rssi: number | null;
}

export interface MapObject {
  id: string;
  layer: string;
  key: string;
  kind: "marker" | "path";
  lat?: number;
  lon?: number;
  alt?: number;
  points?: [number, number, number][];
  label?: string;
  color?: string;
  approximate?: boolean;
  t?: number;
}

export interface StateValue {
  v: unknown;
  t: number;
}

export interface ActionState {
  command?: string;
  status?: string;
  feedback?: unknown;
  result?: unknown;
}

export interface Profile {
  name: string;
  mavlink?: { listen: number; forward?: number | null } | null;
  video?: { listen: number; forward?: number | null } | null;
  bridge?: string | null;
  plugin?: string | null;
}

export interface BridgeStatus {
  url: string;
  online: boolean;
  error: string | null;
  bytes_per_s: number;
  info: {
    commands: Record<string, { available: boolean; error?: string }>;
    unavailable: Record<string, Record<string, string>>;
  } | null;
}

export interface Status {
  profile: Profile | null;
  bridge: BridgeStatus | null;
  video: { packets: number; bytes: number } | null;
  plugin_warning: string | null;
}

export interface ValueSpec {
  type?: "number" | "integer" | "boolean" | "enum" | "string" | "point" | "points";
  label?: string;
  default?: unknown;
  min?: number;
  max?: number;
  values?: (string | number)[];
  unit?: string;
}

export interface CommandSpec {
  label: string;
  description?: string;
  confirm?: boolean | string;
  args?: Record<string, ValueSpec>;
  ros?: unknown;
  mavlink?: unknown;
}

export interface WidgetSpec {
  type: "button" | "form" | "value" | "gauge" | "chart" | "status" | "table" | "events" | "image";
  label?: string;
  command?: string;
  args?: Record<string, unknown>;
  state?: string;
  states?: string[];
  image?: string;
  min?: number;
  max?: number;
  on_text?: string;
  off_text?: string;
  window_s?: number;
}

export interface PanelSpec {
  id: string;
  title: string;
  type?: "custom";
  entry?: string;
  widgets?: WidgetSpec[];
}

export interface MapToolSpec {
  id: string;
  kind: "point" | "path";
  label: string;
  command?: string;
}

export interface LayerSpec {
  id: string;
  label?: string;
  style?: { color?: string };
}

export interface Plugin {
  id: string;
  name: string;
  version?: string;
  description?: string;
  origin: "uploaded" | "directory";
  sha256: string;
  config?: Record<string, ValueSpec>;
  config_values: Record<string, unknown>;
  commands?: Record<string, CommandSpec>;
  state?: Record<string, { label?: string; unit?: string }>;
  images?: Record<string, { label?: string }>;
  map?: { layers?: LayerSpec[] };
  ui?: { panels?: PanelSpec[]; map_tools?: MapToolSpec[] };
}

export interface MapSource {
  id: string;
  label: string;
  offline: boolean;
  attribution: string;
  max_zoom: number;
  tiles: string;
}

export interface CommandResult {
  status: string;
  error?: string;
  subscribers?: number;
  result?: unknown;
  action_id?: string;
  duplicate?: boolean;
}

export interface Plan {
  name: string;
  plan: {
    type: "Feature";
    properties: Record<string, unknown>;
    geometry: { type: "Point" | "LineString"; coordinates: number[] | number[][] };
  };
}
