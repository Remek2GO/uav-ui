import { useSyncExternalStore } from "react";
import type {
  ActionState,
  EventItem,
  MapObject,
  Plugin,
  StateValue,
  Status,
  Telemetry,
} from "./types";

const MAX_EVENTS = 500;
const HISTORY_S = 3600;

export interface Sample {
  t: number;
  v: number;
}

export interface AppState {
  socketOnline: boolean;
  status: Status;
  telemetry: Telemetry | null;
  state: Record<string, StateValue>;
  objects: Record<string, MapObject>;
  actions: Record<string, ActionState>;
  events: EventItem[];
  history: Record<string, Sample[]>;
  plugins: Plugin[];
  pluginProblems: { path: string; error: string }[];
  /** Layers set by custom panels in this browser. */
  localObjects: Record<string, MapObject>;
  /** Chosen plugin. Works with no connection: plans can be prepared before reaching the field. */
  activePlugin: string | null;
}

const ACTIVE_PLUGIN_KEY = "uav-ui-plugin";

function readActivePlugin() {
  try {
    return localStorage.getItem(ACTIVE_PLUGIN_KEY);
  } catch {
    return null;
  }
}

export function setActivePlugin(id: string | null) {
  setStore({ activePlugin: id });
  try {
    if (id === null) localStorage.removeItem(ACTIVE_PLUGIN_KEY);
    else localStorage.setItem(ACTIVE_PLUGIN_KEY, id);
  } catch {
    // only a convenience
  }
}

let current: AppState = {
  socketOnline: false,
  status: { profile: null, bridge: null, video: null, plugin_warning: null },
  telemetry: null,
  state: {},
  objects: {},
  actions: {},
  events: [],
  history: {},
  plugins: [],
  pluginProblems: [],
  localObjects: {},
  activePlugin: readActivePlugin(),
};

const listeners = new Set<() => void>();

export function setStore(update: Partial<AppState>) {
  current = { ...current, ...update };
  listeners.forEach((listener) => listener());
}

export function getStore() {
  return current;
}

export function useStore<T>(selector: (state: AppState) => T): T {
  return useSyncExternalStore(
    (listener) => {
      listeners.add(listener);
      return () => listeners.delete(listener);
    },
    () => selector(current),
  );
}

function withHistory(
  history: Record<string, Sample[]>,
  values: Record<string, StateValue>,
): Record<string, Sample[]> {
  const next = { ...history };
  const cutoff = Date.now() / 1000 - HISTORY_S;
  for (const [key, item] of Object.entries(values)) {
    if (typeof item.v !== "number") continue;
    const samples = next[key] ?? [];
    next[key] = [...samples.filter((s) => s.t >= cutoff), { t: item.t, v: item.v }];
  }
  return next;
}

function mergeObjects(objects: Record<string, MapObject>, list: MapObject[]) {
  const next = { ...objects };
  for (const item of list) next[item.id] = item;
  return next;
}

function byId<T extends { id: string }>(items: T[]): T[] {
  const seen = new Set<string>();
  return items.filter((item) => !seen.has(item.id) && seen.add(item.id));
}

interface ServerMessage {
  type: string;
  status?: Status;
  telemetry?: Telemetry | null;
  state?: Record<string, StateValue>;
  objects?: MapObject[];
  actions?: Record<string, ActionState>;
  events?: EventItem[];
}

function handleMessage(message: ServerMessage) {
  if (message.type === "snapshot") {
    const state = message.state ?? {};
    setStore({
      status: message.status!,
      telemetry: message.telemetry ?? null,
      state,
      objects: mergeObjects({}, message.objects ?? []),
      actions: message.actions ?? {},
      events: byId(message.events ?? []).slice(-MAX_EVENTS),
      history: withHistory({}, state),
    });
    return;
  }

  const update: Partial<AppState> = {};
  if (message.telemetry !== undefined) update.telemetry = message.telemetry;
  if (message.type === "status" && message.status) update.status = message.status;
  if (message.type === "bridge" && message.status !== undefined) {
    update.status = { ...current.status, bridge: message.status as never };
  }
  if (message.state) {
    update.state = { ...current.state, ...message.state };
    update.history = withHistory(current.history, message.state);
  }
  if (message.objects) update.objects = mergeObjects(current.objects, message.objects);
  if (message.actions) update.actions = { ...current.actions, ...message.actions };
  if (message.events) {
    update.events = byId([...current.events, ...message.events]).slice(-MAX_EVENTS);
  }
  setStore(update);
}

export function connectSocket() {
  let socket: WebSocket | null = null;
  let stopped = false;
  let retry: number | undefined;

  const open = () => {
    const scheme = location.protocol === "https:" ? "wss" : "ws";
    socket = new WebSocket(`${scheme}://${location.host}/ws`);
    socket.onopen = () => setStore({ socketOnline: true });
    socket.onmessage = (event) => handleMessage(JSON.parse(event.data));
    socket.onclose = () => {
      setStore({ socketOnline: false });
      if (!stopped) retry = window.setTimeout(open, 1500);
    };
  };
  open();

  return () => {
    stopped = true;
    window.clearTimeout(retry);
    socket?.close();
  };
}
