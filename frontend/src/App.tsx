import { DockviewApi, DockviewReact, DockviewReadyEvent, IDockviewPanelProps } from "dockview";
import { useEffect, useRef, useState } from "react";
import { refreshPlugins } from "./api";
import { ConfirmHost } from "./Confirm";
import { FlightStrip } from "./FlightStrip";
import { EventsPanel } from "./panels/EventsPanel";
import { MapPanel } from "./panels/MapPanel";
import { PluginPanel } from "./panels/PluginPanel";
import { SettingsPanel } from "./panels/SettingsPanel";
import { VideoPanel } from "./panels/VideoPanel";
import { connectSocket, getStore, setActivePlugin, useStore } from "./store";

// Bump when the panels change in a way that old saved layouts cannot express.
const LAYOUT_VERSION = 3;

function stored(key: string) {
  try {
    return localStorage.getItem(key);
  } catch {
    return null;
  }
}

function store(key: string, value: string | null) {
  try {
    if (value === null) localStorage.removeItem(key);
    else localStorage.setItem(key, value);
  } catch {
    // layout and selection are conveniences, the app works without them
  }
}

const CORE_PANELS = [
  { id: "map", component: "map", title: "Mapa" },
  { id: "video", component: "video", title: "Obraz" },
  { id: "events", component: "events", title: "Zdarzenia" },
  { id: "settings", component: "settings", title: "Połączenie" },
];

function defaultLayout(api: DockviewApi) {
  api.addPanel({ id: "map", component: "map", title: "Mapa" });
  api.addPanel({
    id: "video",
    component: "video",
    title: "Obraz",
    position: { referencePanel: "map", direction: "right" },
    initialWidth: 490,
  });
  api.addPanel({
    id: "settings",
    component: "settings",
    title: "Połączenie",
    position: { referencePanel: "video", direction: "below" },
  });
  api.addPanel({
    id: "events",
    component: "events",
    title: "Zdarzenia",
    position: { referencePanel: "map", direction: "below" },
    initialHeight: 150,
  });
}

const layoutKey = (plugin: string | null) => `uav-ui-layout:${LAYOUT_VERSION}:${plugin ?? "_core"}`;

const PANEL_COMPONENTS = {
  map: () => <MapPanel />,
  video: () => <VideoPanel />,
  events: () => <EventsPanel />,
  settings: () => <SettingsPanel />,
  plugin: (props: IDockviewPanelProps<{ pluginId: string; panelId: string }>) => (
    <PluginPanel pluginId={props.params.pluginId} panelId={props.params.panelId} />
  ),
};

function openPluginPanel(dock: DockviewApi, pluginId: string, panelId: string, title: string) {
  const id = `plugin:${pluginId}:${panelId}`;
  if (dock.getPanel(id)) return;
  const reference = dock.panels.find((p) => p.id.startsWith("plugin:")) ?? dock.getPanel("settings");
  dock.addPanel({
    id,
    component: "plugin",
    title,
    params: { pluginId, panelId },
    position: reference ? { referencePanel: reference.id, direction: "within" } : undefined,
    inactive: true,
  });
}

export function App() {
  const status = useStore((s) => s.status);
  const plugins = useStore((s) => s.plugins);
  const activePlugin = useStore((s) => s.activePlugin);

  const dockRef = useRef<DockviewApi | null>(null);
  const [menuOpen, setMenuOpen] = useState(false);
  const [dockReady, setDockReady] = useState(false);

  useEffect(() => connectSocket(), []);
  useEffect(() => {
    void refreshPlugins();
  }, []);

  // After a page reload while connected, the server knows best which plugin is in use.
  const connectedPlugin = status.profile?.plugin ?? null;
  useEffect(() => {
    if (connectedPlugin && connectedPlugin !== getStore().activePlugin) {
      setActivePlugin(connectedPlugin);
    }
  }, [connectedPlugin]);

  const plugin = plugins.find((p) => p.id === activePlugin);

  const onReady = (event: DockviewReadyEvent) => {
    dockRef.current = event.api;
    const saved = stored(layoutKey(getStore().activePlugin));
    let restored = false;
    if (saved) {
      try {
        event.api.fromJSON(JSON.parse(saved));
        restored = event.api.panels.length > 0;
      } catch {
        event.api.clear();
      }
    }
    if (!restored) defaultLayout(event.api);
    event.api.onDidLayoutChange(() => {
      if (dockRef.current?.panels.length) {
        store(layoutKey(getStore().activePlugin), JSON.stringify(event.api.toJSON()));
      }
    });
    setDockReady(true);
  };

  // Plugin panels follow the active plugin: its panels are opened, those of others are closed.
  useEffect(() => {
    const dock = dockRef.current;
    if (!dock || !dockReady) return;
    for (const panel of [...dock.panels]) {
      const params = panel.params as { pluginId?: string } | undefined;
      if (params?.pluginId && params.pluginId !== activePlugin) panel.api.close();
    }
    if (!plugin) return;
    const known = new Set(plugin.ui?.panels?.map((p) => `plugin:${plugin.id}:${p.id}`));
    for (const panel of [...dock.panels]) {
      if (panel.id.startsWith(`plugin:${plugin.id}:`) && !known.has(panel.id)) panel.api.close();
    }
    for (const panel of plugin.ui?.panels ?? []) openPluginPanel(dock, plugin.id, panel.id, panel.title);
  }, [dockReady, plugin, activePlugin]);

  const openPanel = (id: string, component: string, title: string) => {
    const dock = dockRef.current;
    setMenuOpen(false);
    if (!dock) return;
    const existing = dock.getPanel(id);
    if (existing) {
      existing.api.setActive();
      return;
    }
    dock.addPanel({ id, component, title });
  };

  const menu = (
    <>
      <button onClick={() => setMenuOpen(!menuOpen)} aria-expanded={menuOpen}>
        Panele
      </button>
      {menuOpen && (
        <div className="menu-list" role="menu">
          {CORE_PANELS.map((panel) => (
            <button
              key={panel.id}
              role="menuitem"
              onClick={() => openPanel(panel.id, panel.component, panel.title)}
            >
              {panel.title}
            </button>
          ))}
          {plugin?.ui?.panels?.map((panel) => (
            <button
              key={panel.id}
              role="menuitem"
              onClick={() => {
                setMenuOpen(false);
                const dock = dockRef.current;
                if (!dock) return;
                openPluginPanel(dock, plugin.id, panel.id, panel.title);
                dock.getPanel(`plugin:${plugin.id}:${panel.id}`)?.api.setActive();
              }}
            >
              {plugin.name}: {panel.title}
            </button>
          ))}
          <button
            role="menuitem"
            onClick={() => {
              setMenuOpen(false);
              store(layoutKey(activePlugin), null);
              location.reload();
            }}
          >
            Przywróć domyślny układ
          </button>
        </div>
      )}
    </>
  );

  return (
    <div className="app">
      <FlightStrip
        menu={menu}
        onOpenConnection={() => openPanel("settings", "settings", "Połączenie")}
      />
      <main className="dock">
        <DockviewReact className="dockview-theme-dark" components={PANEL_COMPONENTS} onReady={onReady} />
      </main>
      <ConfirmHost />
    </div>
  );
}
