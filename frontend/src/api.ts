import { setStore } from "./store";
import type { CommandResult, MapSource, Plan, Plugin, Profile, Status } from "./types";

export class ApiError extends Error {}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(path, init);
  if (!response.ok) {
    let detail = `${response.status} ${response.statusText}`;
    try {
      const body = await response.json();
      detail = body.detail ?? body.error ?? detail;
    } catch {
      // not JSON: the status line is all there is
    }
    throw new ApiError(detail);
  }
  return response.json();
}

const json = (method: string, body: unknown): RequestInit => ({
  method,
  headers: { "Content-Type": "application/json" },
  body: JSON.stringify(body),
});

export const api = {
  profiles: () => request<Profile[]>("/api/profiles"),
  saveProfiles: (profiles: Profile[]) => request("/api/profiles", json("PUT", profiles)),
  connect: (name: string, plugin: string | null) =>
    request<Status>("/api/connect", json("POST", { name, plugin })),
  disconnect: () => request("/api/disconnect", { method: "POST" }),

  flight: (name: string) =>
    request<CommandResult>(`/api/flight/${name}`, json("POST", { confirmed: true })),

  command: (pluginId: string, name: string, args: Record<string, unknown>, confirmed: boolean) =>
    request<CommandResult>(
      `/api/plugins/${pluginId}/commands/${name}`,
      json("POST", { args, confirmed }),
    ),
  cancelAction: (id: string) =>
    request<CommandResult>(`/api/actions/${id}/cancel`, { method: "POST" }),

  saveConfig: (pluginId: string, values: Record<string, unknown>) =>
    request(`/api/plugins/${pluginId}/config`, json("PUT", values)),
  uploadPlugin: (file: File) => {
    const form = new FormData();
    form.append("file", file);
    return request<{ id: string; name: string }>("/api/plugins", { method: "POST", body: form });
  },
  deletePlugin: (id: string) => request(`/api/plugins/${id}`, { method: "DELETE" }),

  plans: (scope: string) => request<Plan[]>(`/api/plans/${scope}`),
  savePlan: (scope: string, name: string, plan: Plan["plan"]) =>
    request(`/api/plans/${scope}/${encodeURIComponent(name)}`, json("PUT", plan)),
  deletePlan: (scope: string, name: string) =>
    request(`/api/plans/${scope}/${encodeURIComponent(name)}`, { method: "DELETE" }),

  mapSources: () => request<{ sources: MapSource[]; global_scope: string }>("/api/map/sources"),
  prefetch: (area: Record<string, unknown>) =>
    request<PrefetchJob>("/api/tiles/prefetch", json("POST", area)),
  prefetchStatus: (id: number) => request<PrefetchJob>(`/api/tiles/prefetch/${id}`),
};

export interface PrefetchJob {
  id: number;
  total: number;
  done: number;
  failed: number;
  finished: boolean;
}

export async function refreshPlugins() {
  const result = await request<{
    plugins: Plugin[];
    problems: { path: string; error: string }[];
  }>("/api/plugins");
  setStore({ plugins: result.plugins, pluginProblems: result.problems });
}

const STATUS_LABELS: Record<string, string> = {
  ok: "wykonano",
  accepted: "przyjęto",
  rejected: "odrzucono",
  denied: "odmowa autopilota",
  error: "błąd",
  no_ack: "brak potwierdzenia",
  no_vehicle: "brak drona",
  no_bridge: "brak połączenia z dronem",
  unsupported: "nieobsługiwana",
  failed: "nieudana",
  temporarily_rejected: "chwilowo odrzucona",
  in_progress: "w toku",
  busy: "zajęte",
};

export function describeResult(result: CommandResult): string {
  const parts = [STATUS_LABELS[result.status] ?? result.status];
  if (result.subscribers !== undefined) parts.push(`odbiorców: ${result.subscribers}`);
  if (result.duplicate) parts.push("powtórzenie, nie wykonano ponownie");
  if (result.error) parts.push(result.error);
  if (result.result !== undefined) parts.push(JSON.stringify(result.result));
  const [first, ...rest] = parts;
  const sentence = first.charAt(0).toUpperCase() + first.slice(1);
  return rest.length ? `${sentence}, ${rest.join(", ")}` : sentence;
}

export function isSuccess(result: CommandResult): boolean {
  return result.status === "ok" || result.status === "accepted";
}
