import { useEffect, useRef, useState } from "react";
import { api, ApiError, refreshPlugins } from "../api";
import { confirmDialog } from "../Confirm";
import { setActivePlugin, useStore } from "../store";
import type { Plugin, Profile } from "../types";
import { ValueInput } from "../widgets";

function readStored(key: string) {
  try {
    return localStorage.getItem(key);
  } catch {
    return null;
  }
}

function numberOrNull(text: string) {
  return text.trim() === "" ? null : Number(text);
}

function ProfileEditor({
  profile,
  onChange,
}: {
  profile: Profile;
  onChange: (profile: Profile) => void;
}) {
  const port = (section: "mavlink" | "video", field: "listen" | "forward", text: string) => {
    const current = profile[section] ?? { listen: 0 };
    const next = { ...current, [field]: numberOrNull(text) };
    const empty = next.listen === null || next.listen === 0;
    onChange({ ...profile, [section]: empty ? null : next });
  };

  return (
    <div className="profile-editor">
      <label className="field">
        <span>Nazwa</span>
        <input value={profile.name} onChange={(e) => onChange({ ...profile, name: e.target.value })} />
      </label>
      <label className="field">
        <span>MAVLink: port nasłuchu (puste = bez MAVLink)</span>
        <input
          type="number"
          value={profile.mavlink?.listen ?? ""}
          onChange={(e) => port("mavlink", "listen", e.target.value)}
        />
      </label>
      <label className="field">
        <span>MAVLink: przekazuj do QGC na port (puste = nie)</span>
        <input
          type="number"
          disabled={!profile.mavlink}
          value={profile.mavlink?.forward ?? ""}
          onChange={(e) => port("mavlink", "forward", e.target.value)}
        />
      </label>
      <label className="field">
        <span>Wideo: port nasłuchu (puste = bez obrazu)</span>
        <input
          type="number"
          value={profile.video?.listen ?? ""}
          onChange={(e) => port("video", "listen", e.target.value)}
        />
      </label>
      <label className="field">
        <span>Wideo: przekazuj do QGC na port (puste = nie)</span>
        <input
          type="number"
          disabled={!profile.video}
          value={profile.video?.forward ?? ""}
          onChange={(e) => port("video", "forward", e.target.value)}
        />
      </label>
      <label className="field">
        <span>Bridge na dronie (puste = bez komend pluginu)</span>
        <input
          placeholder="http://10.5.0.2:8765"
          value={profile.bridge ?? ""}
          onChange={(e) => onChange({ ...profile, bridge: e.target.value.trim() || null })}
        />
      </label>
    </div>
  );
}

function PluginConfig({ plugin }: { plugin: Plugin }) {
  const specs = plugin.config ?? {};
  const [values, setValues] = useState(plugin.config_values);
  const [message, setMessage] = useState<{ ok: boolean; text: string } | null>(null);

  useEffect(() => setValues(plugin.config_values), [plugin.config_values]);

  if (Object.keys(specs).length === 0) {
    return <div className="muted small-text">Ten plugin nie ma ustawień.</div>;
  }

  const save = async (event: React.FormEvent) => {
    event.preventDefault();
    try {
      await api.saveConfig(plugin.id, values);
      setMessage({ ok: true, text: "Zapisano." });
      await refreshPlugins();
    } catch (error) {
      if (!(error instanceof ApiError)) throw error;
      setMessage({ ok: false, text: error.message });
    }
  };

  return (
    <form className="form" onSubmit={(event) => void save(event)}>
      {Object.entries(specs).map(([name, spec]) => (
        <ValueInput
          key={name}
          name={name}
          spec={spec}
          value={values[name]}
          onChange={(value) => setValues((current) => ({ ...current, [name]: value }))}
        />
      ))}
      <button type="submit">Zapisz ustawienia</button>
      {message && <div className={`result ${message.ok ? "ok" : "bad"}`}>{message.text}</div>}
    </form>
  );
}

export function SettingsPanel() {
  const activePlugin = useStore((s) => s.activePlugin);
  const status = useStore((s) => s.status);
  const plugins = useStore((s) => s.plugins);
  const problems = useStore((s) => s.pluginProblems);
  const connected = status.profile !== null;

  const [profiles, setProfiles] = useState<Profile[]>([]);
  const [selected, setSelected] = useState(() => readStored("uav-ui-profile") ?? "");
  const [editing, setEditing] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const fileInput = useRef<HTMLInputElement>(null);

  useEffect(() => {
    api
      .profiles()
      .then(setProfiles)
      .catch((e: Error) => setError(e.message));
    void refreshPlugins();
  }, []);

  const profile = profiles.find((p) => p.name === selected) ?? profiles[0];
  const unavailable = status.bridge?.info?.unavailable;
  const unavailableList = unavailable
    ? Object.entries(unavailable).flatMap(([kind, items]) =>
        Object.entries(items).map(([name, why]) => `${kind}/${name}: ${why}`),
      )
    : [];

  const guard = async (action: () => Promise<unknown>) => {
    setError(null);
    setNotice(null);
    setBusy(true);
    try {
      await action();
    } catch (e) {
      if (!(e instanceof ApiError)) throw e;
      setError(e.message);
    } finally {
      setBusy(false);
    }
  };

  const connect = () =>
    guard(async () => {
      if (!profile) return;
      if (profile.mavlink) {
        // Seen in simulation: PX4 refuses to arm with no GCS, and the hydrolab mission keeps trying,
        // so the heartbeat of this GUI is what lets the drone arm and take off. QGC does the same.
        const confirmed = await confirmDialog(
          "Połączyć z dronem?",
          "GUI zacznie wysyłać heartbeat stacji naziemnej. Jeśli misja na dronie czeka na uzbrojenie, " +
            "a PX4 wstrzymywał je z powodu braku stacji, dron może się teraz uzbroić i wystartować.",
          true,
        );
        if (!confirmed) return;
      }
      try {
        localStorage.setItem("uav-ui-profile", profile.name);
      } catch {
        // only a convenience
      }
      await api.connect(profile.name, activePlugin);
    });

  const updateProfile = (updated: Profile) => {
    setProfiles(profiles.map((p) => (p === profile ? updated : p)));
    setSelected(updated.name);
  };

  const saveProfiles = () =>
    guard(async () => {
      await api.saveProfiles(profiles);
      setEditing(false);
      setNotice("Zapisano profile.");
    });

  const addProfile = () => {
    const name = `Profil ${profiles.length + 1}`;
    setProfiles([...profiles, { name, mavlink: null, video: null, bridge: null, plugin: null }]);
    setSelected(name);
    setEditing(true);
  };

  const removeProfile = () =>
    guard(async () => {
      if (!profile) return;
      if (!(await confirmDialog("Usunąć profil?", `Profil „${profile.name}” zostanie usunięty.`))) {
        return;
      }
      const remaining = profiles.filter((p) => p !== profile);
      await api.saveProfiles(remaining);
      setProfiles(remaining);
      setEditing(false);
    });

  const upload = (file: File | undefined) =>
    guard(async () => {
      if (!file) return;
      const result = await api.uploadPlugin(file);
      await refreshPlugins();
      setActivePlugin(result.id);
      setNotice(`Wgrano plugin ${result.name}.`);
      if (fileInput.current) fileInput.current.value = "";
    });

  const removePlugin = (plugin: Plugin) =>
    guard(async () => {
      const text = `Plugin „${plugin.name}” i jego ustawienia zostaną usunięte. Zapisane plany zostają.`;
      if (!(await confirmDialog("Usunąć plugin?", text))) return;
      await api.deletePlugin(plugin.id);
      if (activePlugin === plugin.id) setActivePlugin(null);
      await refreshPlugins();
    });

  const active = plugins.find((p) => p.id === activePlugin);

  return (
    <div className="panel settings">
      <section>
        <h3>Połączenie</h3>
        <label className="field">
          <span>Profil</span>
          <select
            value={profile?.name ?? ""}
            disabled={connected}
            onChange={(e) => {
              setSelected(e.target.value);
              setEditing(false);
            }}
          >
            {profiles.map((p) => (
              <option key={p.name} value={p.name}>
                {p.name}
              </option>
            ))}
          </select>
        </label>
        <label className="field">
          <span>Plugin</span>
          <select
            value={activePlugin ?? ""}
            disabled={connected}
            onChange={(e) => setActivePlugin(e.target.value || null)}
          >
            <option value="">bez pluginu</option>
            {plugins.map((p) => (
              <option key={p.id} value={p.id}>
                {p.name} {p.version ?? ""}
              </option>
            ))}
          </select>
        </label>
        <div className="card-row">
          {connected ? (
            <button disabled={busy} onClick={() => void guard(() => api.disconnect())}>
              Rozłącz
            </button>
          ) : (
            <button className="primary" disabled={busy || !profile} onClick={() => void connect()}>
              Połącz
            </button>
          )}
          <button disabled={connected} onClick={() => setEditing(!editing)}>
            {editing ? "Zwiń" : "Edytuj profil"}
          </button>
          <button disabled={connected} onClick={addProfile}>
            Nowy
          </button>
        </div>
        {editing && profile && (
          <>
            <ProfileEditor profile={profile} onChange={updateProfile} />
            <div className="card-row">
              <button className="primary" onClick={() => void saveProfiles()}>
                Zapisz profile
              </button>
              <button onClick={() => void removeProfile()}>Usuń profil</button>
            </div>
          </>
        )}
        {error && <div className="result bad">{error}</div>}
        {notice && <div className="result ok">{notice}</div>}
        {status.plugin_warning && <div className="result bad">{status.plugin_warning}</div>}
        {status.bridge && (
          <div className="muted small-text">
            Bridge {status.bridge.url}:{" "}
            {status.bridge.online
              ? `działa, ${status.bridge.bytes_per_s} B/s przez łącze`
              : `brak połączenia${status.bridge.error ? ` (${status.bridge.error})` : ""}`}
          </div>
        )}
        {unavailableList.length > 0 && (
          <details className="small-text">
            <summary className="result bad">
              Niedostępne na dronie: {unavailableList.length}
            </summary>
            <ul>
              {unavailableList.map((item) => (
                <li key={item}>{item}</li>
              ))}
            </ul>
          </details>
        )}
      </section>

      <section>
        <h3>Pluginy</h3>
        <label className={`file-button ${busy ? "disabled" : ""}`}>
          Wgraj plugin (plugin.yaml albo zip)
          <input
            ref={fileInput}
            className="sr-only"
            type="file"
            accept=".yaml,.yml,.zip"
            aria-label="Wgraj plugin: plugin.yaml albo zip"
            disabled={busy}
            onChange={(e) => void upload(e.target.files?.[0])}
          />
        </label>
        {plugins.length === 0 && (
          <div className="muted small-text">
            Nie ma żadnego pluginu. GUI działa bez nich: mapa, obraz, stan drona i komendy lotu.
          </div>
        )}
        {plugins.map((plugin) => (
          <div className="card-row" key={plugin.id}>
            <span className="grow">
              {plugin.name} <span className="muted">{plugin.version}</span>
              {plugin.origin === "directory" && <span className="muted"> (katalog)</span>}
            </span>
            {plugin.origin === "uploaded" && (
              <button className="small" onClick={() => void removePlugin(plugin)}>
                Usuń
              </button>
            )}
          </div>
        ))}
        {problems.map((problem) => (
          <div className="result bad small-text" key={problem.path}>
            {problem.path}: {problem.error}
          </div>
        ))}
      </section>

      {active && (
        <section>
          <h3>Ustawienia: {active.name}</h3>
          {active.description && <div className="muted small-text">{active.description}</div>}
          <PluginConfig plugin={active} />
        </section>
      )}
    </div>
  );
}
