import { useEffect, useMemo, useRef, useState } from "react";
import { api, describeResult, isSuccess } from "./api";
import { runPluginCommand } from "./commands";
import { EventList } from "./panels/EventsPanel";
import { useStore } from "./store";
import type { CommandResult, CommandSpec, Plugin, ValueSpec, WidgetSpec } from "./types";

const SERIES_COLORS = ["#45c8f0", "#f0a830", "#62c682", "#ff8d7a", "#dfe5da"];

function formatValue(value: unknown, unit?: string) {
  if (value === undefined || value === null) return "–";
  const text = typeof value === "number" ? String(Math.round(value * 1000) / 1000) : String(value);
  return unit ? `${text} ${unit}` : text;
}

function useCommandAvailability(commandName?: string) {
  return useStore((s) => {
    if (!commandName) return undefined;
    const entry = s.status.bridge?.info?.commands?.[commandName];
    return entry?.available === false ? entry.error : undefined;
  });
}

function ResultLine({ result }: { result: CommandResult | null }) {
  if (!result || result.status === "cancelled") return null;
  return (
    <div className={`result ${isSuccess(result) ? "ok" : "bad"}`} role="status">
      {describeResult(result)}
    </div>
  );
}

function ActionLine({ actionId }: { actionId?: string }) {
  const action = useStore((s) => (actionId ? s.actions[actionId] : undefined));
  if (!actionId || !action) return null;
  const running = action.status === "running" || action.status === "sending";
  return (
    <div className="action-line">
      <span className="chip">{action.status}</span>
      <code>{JSON.stringify(action.result ?? action.feedback ?? null)}</code>
      {/* type="button": this sits inside a form, and a default button would submit it again. */}
      {running && (
        <button type="button" className="small" onClick={() => void api.cancelAction(actionId)}>
          Anuluj
        </button>
      )}
    </div>
  );
}

function CommandButton({ plugin, widget }: { plugin: Plugin; widget: WidgetSpec }) {
  const [result, setResult] = useState<CommandResult | null>(null);
  const [busy, setBusy] = useState(false);
  const unavailable = useCommandAvailability(widget.command);
  const command = plugin.commands?.[widget.command!];
  if (!command) return null;

  const run = async () => {
    setBusy(true);
    setResult(null);
    try {
      setResult(await runPluginCommand(plugin, widget.command!, widget.args ?? {}));
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="widget">
      <button
        disabled={busy || unavailable !== undefined}
        title={unavailable ?? command.description}
        onClick={() => void run()}
      >
        {widget.label ?? command.label}
      </button>
      {unavailable && <div className="result bad">Niedostępne: {unavailable}</div>}
      <ResultLine result={result} />
      <ActionLine actionId={result?.action_id} />
    </div>
  );
}

function initialValue(spec: ValueSpec, config: Record<string, unknown>): unknown {
  const preset = spec.default;
  if (typeof preset === "string" && preset.startsWith("$config.")) {
    return config[preset.slice("$config.".length)];
  }
  if (preset !== undefined) return preset;
  if (spec.type === "enum") return spec.values?.[0];
  if (spec.type === "boolean") return false;
  if (spec.type === "enums") return [];
  return "";
}

export function ValueInput({
  spec,
  value,
  onChange,
  name,
}: {
  spec: ValueSpec;
  value: unknown;
  onChange: (value: unknown) => void;
  name: string;
}) {
  const label = spec.label ?? name;
  if (spec.type === "enums") {
    const selected = Array.isArray(value) ? (value as unknown[]) : [];
    return (
      <fieldset className="field choices">
        <legend>{label}</legend>
        {(spec.values ?? []).map((option) => (
          <label key={String(option)} className="check">
            <input
              type="checkbox"
              checked={selected.includes(option)}
              onChange={(event) =>
                onChange(
                  event.target.checked
                    ? [...selected, option]
                    : selected.filter((item) => item !== option),
                )
              }
            />
            {String(option)}
          </label>
        ))}
      </fieldset>
    );
  }
  if (spec.type === "boolean") {
    return (
      <label className="field check">
        <input
          type="checkbox"
          checked={Boolean(value)}
          onChange={(event) => onChange(event.target.checked)}
        />
        {label}
      </label>
    );
  }
  if (spec.type === "enum") {
    return (
      <label className="field">
        <span>{label}</span>
        <select value={String(value ?? "")} onChange={(event) => onChange(event.target.value)}>
          {(value === "" || value === undefined) && <option value="">wybierz</option>}
          {(spec.values ?? []).map((option) => (
            <option key={String(option)} value={String(option)}>
              {String(option)}
            </option>
          ))}
        </select>
      </label>
    );
  }
  const numeric = spec.type === "number" || spec.type === "integer";
  return (
    <label className="field">
      <span>
        {label}
        {spec.unit ? ` [${spec.unit}]` : ""}
      </span>
      <input
        type={numeric ? "number" : "text"}
        value={value === undefined || value === null ? "" : String(value)}
        min={spec.min}
        max={spec.max}
        step={spec.type === "integer" ? 1 : "any"}
        onChange={(event) => {
          const text = event.target.value;
          onChange(numeric ? (text === "" ? "" : Number(text)) : text);
        }}
      />
    </label>
  );
}

function CommandForm({ plugin, widget }: { plugin: Plugin; widget: WidgetSpec }) {
  const command: CommandSpec | undefined = plugin.commands?.[widget.command!];
  const specs = command?.args ?? {};
  const signature = JSON.stringify(plugin.config_values);
  const [values, setValues] = useState<Record<string, unknown>>({});
  const [result, setResult] = useState<CommandResult | null>(null);
  const [busy, setBusy] = useState(false);
  const unavailable = useCommandAvailability(widget.command);

  useEffect(() => {
    setValues(
      Object.fromEntries(
        Object.entries(specs).map(([name, spec]) => [
          name,
          initialValue(spec, plugin.config_values),
        ]),
      ),
    );
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [widget.command, signature]);

  if (!command) return null;

  const missing = Object.entries(specs)
    .filter(([name]) => values[name] === "" || values[name] === undefined)
    .map(([name, spec]) => spec.label ?? name);

  const submit = async (event: React.FormEvent) => {
    event.preventDefault();
    setBusy(true);
    setResult(null);
    try {
      const args = { ...values, ...(widget.args ?? {}) };
      setResult(await runPluginCommand(plugin, widget.command!, args));
    } finally {
      setBusy(false);
    }
  };

  return (
    <form className="widget form" onSubmit={(event) => void submit(event)}>
      <div className="form-title">{widget.label ?? command.label}</div>
      {command.description && <div className="muted small-text">{command.description}</div>}
      {Object.entries(specs).map(([name, spec]) => (
        <ValueInput
          key={name}
          name={name}
          spec={spec}
          value={values[name]}
          onChange={(value) => setValues((current) => ({ ...current, [name]: value }))}
        />
      ))}
      <button
        type="submit"
        className="primary"
        disabled={busy || missing.length > 0 || unavailable !== undefined}
        title={unavailable ?? (missing.length ? `Uzupełnij: ${missing.join(", ")}` : undefined)}
      >
        {widget.submit_label ?? "Wyślij"}
      </button>
      {unavailable && <div className="result bad">Niedostępne: {unavailable}</div>}
      <ResultLine result={result} />
      <ActionLine actionId={result?.action_id} />
    </form>
  );
}

function ValueWidget({ plugin, widget }: { plugin: Plugin; widget: WidgetSpec }) {
  const spec = plugin.state?.[widget.state!];
  const item = useStore((s) => s.state[widget.state!]);
  return (
    <div className="widget value-widget">
      <span className="muted">{widget.label ?? spec?.label ?? widget.state}</span>
      <span className="value">{formatValue(item?.v, spec?.unit)}</span>
    </div>
  );
}

function StatusWidget({ plugin, widget }: { plugin: Plugin; widget: WidgetSpec }) {
  const spec = plugin.state?.[widget.state!];
  const item = useStore((s) => s.state[widget.state!]);
  const known = item !== undefined;
  const on = Boolean(item?.v);
  return (
    <div className="widget value-widget">
      <span className="muted">{widget.label ?? spec?.label ?? widget.state}</span>
      <span className={`chip ${known ? (on ? "chip-on" : "") : "chip-unknown"}`}>
        {known ? (on ? (widget.on_text ?? "Tak") : (widget.off_text ?? "Nie")) : "brak danych"}
      </span>
    </div>
  );
}

function GaugeWidget({ plugin, widget }: { plugin: Plugin; widget: WidgetSpec }) {
  const spec = plugin.state?.[widget.state!];
  const item = useStore((s) => s.state[widget.state!]);
  const min = widget.min ?? 0;
  const max = widget.max ?? 100;
  const numeric = typeof item?.v === "number" ? item.v : null;
  const ratio = numeric === null ? 0 : Math.min(1, Math.max(0, (numeric - min) / (max - min)));
  return (
    <div className="widget">
      <div className="value-widget">
        <span className="muted">{widget.label ?? spec?.label ?? widget.state}</span>
        <span className="value">{formatValue(item?.v, spec?.unit)}</span>
      </div>
      <div
        className="gauge"
        role="meter"
        aria-valuemin={min}
        aria-valuemax={max}
        aria-valuenow={numeric ?? undefined}
      >
        <div style={{ width: `${ratio * 100}%` }} />
      </div>
      <div className="gauge-scale muted small-text">
        <span>{min}</span>
        <span>{max}</span>
      </div>
    </div>
  );
}

function TableWidget({ plugin, widget }: { plugin: Plugin; widget: WidgetSpec }) {
  const state = useStore((s) => s.state);
  return (
    <table className="widget table">
      <tbody>
        {(widget.states ?? []).map((key) => {
          const spec = plugin.state?.[key];
          return (
            <tr key={key}>
              <th scope="row">{spec?.label ?? key}</th>
              <td>{formatValue(state[key]?.v, spec?.unit)}</td>
            </tr>
          );
        })}
      </tbody>
    </table>
  );
}

function ChartWidget({ plugin, widget }: { plugin: Plugin; widget: WidgetSpec }) {
  const keys = widget.states ?? [];
  const history = useStore((s) => s.history);
  const windowS = widget.window_s ?? 120;
  const width = 320;
  const height = 120;
  const pad = { left: 34, right: 8, top: 8, bottom: 16 };

  const { series, min, max, end } = useMemo(() => {
    const newest = Math.max(
      Date.now() / 1000 - 1,
      ...keys.map((key) => history[key]?.at(-1)?.t ?? 0),
    );
    const cut = newest - windowS;
    const visible = keys.map((key) => (history[key] ?? []).filter((sample) => sample.t >= cut));
    const all = visible.flat().map((sample) => sample.v);
    let low = all.length ? Math.min(...all) : 0;
    let high = all.length ? Math.max(...all) : 1;
    if (high - low < 1e-6) {
      low -= 1;
      high += 1;
    }
    return { series: visible, min: low, max: high, end: newest };
  }, [history, keys, windowS]);

  const x = (t: number) =>
    pad.left + ((t - (end - windowS)) / windowS) * (width - pad.left - pad.right);
  const y = (v: number) =>
    pad.top + (1 - (v - min) / (max - min)) * (height - pad.top - pad.bottom);

  return (
    <div className="widget">
      {widget.label && <div className="form-title">{widget.label}</div>}
      {series.every((samples) => samples.length === 0) && (
        <div className="muted small-text">Na razie nie ma danych do wykresu.</div>
      )}
      <svg viewBox={`0 0 ${width} ${height}`} className="chart" role="img" aria-label="Wykres">
        <line x1={pad.left} x2={width - pad.right} y1={y(max)} y2={y(max)} className="grid" />
        <line x1={pad.left} x2={width - pad.right} y1={y(min)} y2={y(min)} className="grid" />
        <text x={pad.left - 4} y={y(max) + 4} textAnchor="end" className="axis">
          {Math.round(max * 100) / 100}
        </text>
        <text x={pad.left - 4} y={y(min) + 4} textAnchor="end" className="axis">
          {Math.round(min * 100) / 100}
        </text>
        <text x={pad.left} y={height - 3} className="axis">
          -{windowS}s
        </text>
        <text x={width - pad.right} y={height - 3} textAnchor="end" className="axis">
          teraz
        </text>
        {series.map((samples, index) => (
          <polyline
            key={keys[index]}
            fill="none"
            stroke={SERIES_COLORS[index % SERIES_COLORS.length]}
            strokeWidth="1.6"
            points={samples.map((s) => `${x(s.t).toFixed(1)},${y(s.v).toFixed(1)}`).join(" ")}
          />
        ))}
      </svg>
      <div className="legend">
        {keys.map((key, index) => (
          <span key={key}>
            <i style={{ background: SERIES_COLORS[index % SERIES_COLORS.length] }} />
            {plugin.state?.[key]?.label ?? key}
          </span>
        ))}
      </div>
    </div>
  );
}

function ImageWidget({ plugin, widget }: { plugin: Plugin; widget: WidgetSpec }) {
  const [src, setSrc] = useState<string | null>(null);
  const urlRef = useRef<string | null>(null);
  const bridgeOnline = useStore((s) => Boolean(s.status.bridge?.online));

  useEffect(() => {
    if (!bridgeOnline) return;
    let stopped = false;
    let etag: string | null = null;

    const load = async () => {
      while (!stopped) {
        try {
          const response = await fetch(`/api/plugins/${plugin.id}/images/${widget.image}`, {
            headers: etag ? { "If-None-Match": etag } : {},
          });
          if (response.status === 200) {
            etag = response.headers.get("ETag");
            const url = URL.createObjectURL(await response.blob());
            if (urlRef.current) URL.revokeObjectURL(urlRef.current);
            urlRef.current = url;
            setSrc(url);
          }
        } catch {
          // the bridge may be out of range for a moment
        }
        await new Promise((resolve) => setTimeout(resolve, 1500));
      }
    };
    void load();
    return () => {
      stopped = true;
    };
  }, [bridgeOnline, plugin.id, widget.image]);

  useEffect(
    () => () => {
      if (urlRef.current) URL.revokeObjectURL(urlRef.current);
    },
    [],
  );

  const label = widget.label ?? plugin.images?.[widget.image!]?.label ?? widget.image;
  return (
    <div className="widget">
      <div className="form-title">{label}</div>
      {src ? (
        <img className="preview" src={src} alt={label} />
      ) : (
        <div className="empty small-text">Brak obrazu</div>
      )}
    </div>
  );
}

export function Widget({ plugin, widget }: { plugin: Plugin; widget: WidgetSpec }) {
  switch (widget.type) {
    case "button":
      return <CommandButton plugin={plugin} widget={widget} />;
    case "form":
      return <CommandForm plugin={plugin} widget={widget} />;
    case "value":
      return <ValueWidget plugin={plugin} widget={widget} />;
    case "status":
      return <StatusWidget plugin={plugin} widget={widget} />;
    case "gauge":
      return <GaugeWidget plugin={plugin} widget={widget} />;
    case "table":
      return <TableWidget plugin={plugin} widget={widget} />;
    case "chart":
      return <ChartWidget plugin={plugin} widget={widget} />;
    case "image":
      return <ImageWidget plugin={plugin} widget={widget} />;
    case "events":
      return <EventList />;
  }
}
