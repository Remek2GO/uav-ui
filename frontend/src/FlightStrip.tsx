import { useState } from "react";
import { api, ApiError, describeResult, isSuccess } from "./api";
import { confirmDialog } from "./Confirm";
import { useStore } from "./store";
import type { CommandResult, Telemetry } from "./types";

const MISSION_WARNING =
  "Misja na dronie nie wie o tej komendzie i może wydać własną komendę lotu, która ją nadpisze. Obserwuj tryb na górze ekranu. Żeby polecieć ponownie, może być potrzebny restart misji.";

// Seen in simulation: RTL or Hold right after take-off left the vehicle armed on the ground for over a
// minute, while Land disarmed it.
const NEAR_GROUND_M = 1;
const NEAR_GROUND_WARNING = " Dron jest przy ziemi: tylko Land go rozbroi.";

const COMMANDS = [
  { id: "hold", label: "Hold", title: "Zawisnąć w miejscu?", text: "Dron przejdzie w tryb HOLD i zawiśnie." },
  {
    id: "rtl",
    label: "RTL",
    title: "Wrócić do miejsca startu?",
    text: "Dron przejdzie w tryb RTL: wróci do miejsca startu i wyląduje.",
  },
  { id: "land", label: "Land", title: "Lądować teraz?", text: "Dron zacznie lądować w miejscu, w którym jest." },
];

const FIX_NAMES: Record<number, string> = {
  0: "brak",
  1: "brak",
  2: "2D",
  3: "3D",
  4: "DGPS",
  5: "RTK float",
  6: "RTK fix",
};

function Readout({
  label,
  value,
  unit,
  tone,
}: {
  label: string;
  value: string;
  unit?: string;
  tone?: "warn" | "bad";
}) {
  return (
    <div className={tone}>
      <dd>
        {value}
        {unit && value !== "–" && <small>{unit}</small>}
      </dd>
      <dt>{label}</dt>
    </div>
  );
}

function fixed(value: number | null | undefined, digits: number) {
  return value === null || value === undefined ? "–" : value.toFixed(digits);
}

function modeBlock(telemetry: Telemetry | null, hasMavlink: boolean, connected: boolean) {
  if (!connected) return { className: "", title: "Bez połączenia", detail: "Mapa i plany działają" };
  if (!hasMavlink) return { className: "", title: "Bez telemetrii", detail: "Profil nie ma MAVLink" };
  if (!telemetry?.online) {
    return telemetry?.mode
      ? { className: "lost", title: "Utracono drona", detail: `Ostatni tryb: ${telemetry.mode}` }
      : { className: "", title: "Czekam na drona", detail: "Brak heartbeatu" };
  }
  return telemetry.armed
    ? { className: "armed", title: telemetry.mode ?? "?", detail: "Uzbrojony" }
    : { className: "disarmed", title: telemetry.mode ?? "?", detail: "Rozbrojony" };
}

function batteryTone(percent: number | null | undefined) {
  if (percent === null || percent === undefined) return undefined;
  if (percent < 20) return "bad" as const;
  return percent < 35 ? ("warn" as const) : undefined;
}

export function FlightStrip({ onOpenConnection, menu }: { onOpenConnection: () => void; menu: React.ReactNode }) {
  const socketOnline = useStore((s) => s.socketOnline);
  const status = useStore((s) => s.status);
  const telemetry = useStore((s) => s.telemetry);
  const plugins = useStore((s) => s.plugins);
  const activePlugin = useStore((s) => s.activePlugin);
  const [result, setResult] = useState<{ name: string; result: CommandResult } | null>(null);
  const [busy, setBusy] = useState(false);

  const profile = status.profile;
  const hasMavlink = Boolean(profile?.mavlink);
  const connected = profile !== null;
  const online = telemetry?.online ?? false;
  const bridge = status.bridge;
  const block = modeBlock(telemetry, hasMavlink, connected);
  const plugin = plugins.find((p) => p.id === activePlugin);

  const reason = !connected
    ? "Najpierw połącz się z dronem"
    : !online
      ? "Brak heartbeatu z autopilota"
      : telemetry?.autopilot !== "px4"
        ? "Komendy lotu obsługują w tej wersji tylko PX4"
        : !telemetry?.armed
          ? "Dron nie jest uzbrojony"
          : undefined;

  const run = async (command: (typeof COMMANDS)[number]) => {
    const nearGround =
      command.id !== "land" && telemetry?.rel_alt !== null && telemetry?.rel_alt !== undefined
        ? telemetry.rel_alt < NEAR_GROUND_M
        : false;
    const text = `${command.text} ${MISSION_WARNING}${nearGround ? NEAR_GROUND_WARNING : ""}`;
    if (!(await confirmDialog(command.title, text, true))) return;
    setBusy(true);
    setResult(null);
    try {
      setResult({ name: command.label, result: await api.flight(command.id) });
    } catch (error) {
      if (!(error instanceof ApiError)) throw error;
      setResult({ name: command.label, result: { status: "error", error: error.message } });
    } finally {
      setBusy(false);
    }
  };

  return (
    <>
      <header className="strip">
        <div className={`mode ${block.className}`}>
          <strong>{block.title}</strong>
          <span>{block.detail}</span>
        </div>

        {hasMavlink ? (
          <dl className="readouts">
            <Readout label="Wysokość" value={fixed(telemetry?.rel_alt, 1)} unit="m" />
            <Readout label="Prędkość" value={fixed(telemetry?.groundspeed, 1)} unit="m/s" />
            <Readout
              label={telemetry?.battery_v ? `Bateria, ${telemetry.battery_v} V` : "Bateria"}
              value={telemetry?.battery_pct === null || telemetry?.battery_pct === undefined ? "–" : String(telemetry.battery_pct)}
              unit="%"
              tone={batteryTone(telemetry?.battery_pct)}
            />
            <Readout
              label="GPS"
              value={
                telemetry?.gps_fix === null || telemetry?.gps_fix === undefined
                  ? "–"
                  : (FIX_NAMES[telemetry.gps_fix] ?? String(telemetry.gps_fix))
              }
              unit={telemetry?.satellites ? ` ${telemetry.satellites} sat.` : undefined}
            />
            <Readout
              label="Pozycja"
              value={telemetry?.lat ? `${telemetry.lat.toFixed(6)}, ${telemetry.lon?.toFixed(6)}` : "–"}
            />
          </dl>
        ) : (
          <div className="strip-note">
            {!connected && (
              <button className="primary" onClick={onOpenConnection}>
                Połącz z dronem
              </button>
            )}
            {connected && <span className="muted">Profil: {profile?.name}</span>}
          </div>
        )}

        <span className="grow" />

        {connected && (
          <div className="links" aria-label="Stan łączy">
            {hasMavlink && (
              <span className={`link ${online ? "link-on" : ""}`}>
                <i />
                Telemetria {online ? "działa" : "brak"}
              </span>
            )}
            {profile?.video && (
              <span className={`link ${status.video?.packets ? "link-on" : ""}`}>
                <i />
                Obraz {status.video?.packets ? "działa" : "brak"}
              </span>
            )}
            {bridge && (
              <span className={`link ${bridge.online ? "link-on" : ""}`} title={bridge.error ?? undefined}>
                <i />
                Komendy {bridge.online ? `działają, ${bridge.bytes_per_s} B/s` : "brak"}
              </span>
            )}
          </div>
        )}

        <div className="flight-commands" role="group" aria-label="Komendy lotu">
          {COMMANDS.map((command) => (
            <button
              key={command.id}
              className="danger"
              disabled={Boolean(reason) || busy}
              title={reason}
              onClick={() => void run(command)}
            >
              {command.label}
            </button>
          ))}
        </div>

        <div className="strip-menu">{menu}</div>
      </header>

      {!socketOnline && (
        <div className="strip-message bad" role="alert">
          Nie ma połączenia z aplikacją. Sprawdź, czy działa kontener uav-ui. Ponawiam połączenie.
        </div>
      )}
      {status.plugin_warning && (
        <div className="strip-message warn" role="status">
          {status.plugin_warning}
        </div>
      )}
      {result && (
        <div className={`strip-message ${isSuccess(result.result) ? "ok" : "bad"}`} role="status">
          <span className="grow">
            {result.name}: {describeResult(result.result)}
          </span>
          <button className="small" onClick={() => setResult(null)}>
            Zamknij
          </button>
        </div>
      )}
      {plugin === undefined && activePlugin && (
        <div className="strip-message warn" role="status">
          Plugin „{activePlugin}” nie jest wgrany. Wgraj go w panelu Połączenie.
        </div>
      )}
    </>
  );
}
