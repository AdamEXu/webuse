import { useCallback, useEffect, useRef, useState } from "react";
import { Link, useMatch, useNavigate } from "react-router-dom";
import {
  API,
  getJSON,
  parseHandle,
  postJSON,
  type AppInfo,
  type InstalledApp,
  type OpenResult,
  type WindowInfo,
} from "../../api";

// One not-running installed app: name + an Open button that launches it and
// navigates to its fresh driver. Label goes Open -> Opening… -> Retry on failure.
function ClosedApp({ a }: { a: InstalledApp }) {
  const navigate = useNavigate();
  const [label, setLabel] = useState("Open");
  const [busy, setBusy] = useState(false);

  async function open() {
    setBusy(true);
    setLabel("Opening…");
    try {
      const j = await postJSON<OpenResult>(API.appsOpen, {
        bundle_id: a.bundle_id,
        path: a.path,
        name: a.name,
      });
      if (j.ok && j.pid) {
        navigate(`/app/${j.pid}`);
        return;
      }
      setBusy(false);
      setLabel("Retry");
    } catch {
      setBusy(false);
      setLabel("Retry");
    }
  }

  return (
    <div className="sb-closed-app">
      <span className="sb-closed-name">{a.name}</span>
      <button className="sb-open" disabled={busy} onClick={open}>
        {label}
      </button>
    </div>
  );
}

// Collapsible catalog of installed-but-not-running apps, lazy-loaded on first
// expand. (Running apps already appear above, so they are filtered out here.)
function ClosedApps() {
  const loaded = useRef(false);
  const [state, setState] = useState<"idle" | "loading" | "loaded" | "error">(
    "idle",
  );
  const [apps, setApps] = useState<InstalledApp[]>([]);

  async function onToggle(e: React.SyntheticEvent<HTMLDetailsElement>) {
    if (!e.currentTarget.open || loaded.current) return;
    loaded.current = true;
    setState("loading");
    try {
      const d = await getJSON<{ apps: InstalledApp[] }>(API.apps(true));
      setApps((d.apps || []).filter((a) => !a.running));
      setState("loaded");
    } catch {
      setState("error");
    }
  }

  return (
    <details id="sb-closed" onToggle={onToggle}>
      <summary className="sb-closed-summary">Show Closed Apps</summary>
      <div className="sb-closed-list">
        {state === "loading" || state === "idle" ? (
          <div className="sb-empty">Loading…</div>
        ) : state === "error" ? (
          <div className="sb-empty">Failed to load.</div>
        ) : apps.length ? (
          apps.map((a) => (
            <ClosedApp a={a} key={a.path || a.bundle_id || a.name} />
          ))
        ) : (
          <div className="sb-empty">No other apps.</div>
        )}
      </div>
    </details>
  );
}

// The persistent left sidebar — the app's primary navigator, mounted in the
// Layout so it survives every route change. The active app/window is derived
// from the ROUTE (not props) so highlighting stays correct as the URL changes.
export default function Sidebar({
  debug,
  setDebug,
}: {
  debug: boolean;
  setDebug: (v: boolean) => void;
}) {
  const navigate = useNavigate();

  // Active handle from the route. On /info (or anywhere off an app) nothing is
  // highlighted; on /app/:handle we parse pid[.window_id] for the current marks.
  const appMatch = useMatch("/app/:handle");
  const onInfo = !!useMatch("/info");
  const onTerminal = !!useMatch("/terminal");
  const active = appMatch?.params.handle
    ? parseHandle(appMatch.params.handle)
    : null;
  const PID = active?.pid ?? 0;
  const WINDOW_ID = active?.windowId ?? null;

  const [apps, setApps] = useState<AppInfo[]>([]);
  const [expanded, setExpanded] = useState<Set<number>>(
    () => new Set(PID ? [PID] : []),
  );
  const [winsByPid, setWinsByPid] = useState<Map<number, WindowInfo[]>>(
    () => new Map(),
  );
  const expandedRef = useRef(expanded);
  expandedRef.current = expanded;

  const loadWins = useCallback(async (pid: number) => {
    try {
      const d = await getJSON<{ windows: WindowInfo[] }>(API.windows(pid));
      const list = d.windows || [];
      setWinsByPid((prev) => new Map(prev).set(pid, list));
    } catch {
      /* transient */
    }
  }, []);

  useEffect(() => {
    getJSON<{ apps: AppInfo[] }>(API.apps())
      .then((d) => setApps(d.apps || []))
      .catch(() => {});

    let es: EventSource | null = null;
    try {
      es = new EventSource(API.appsEvents);
      es.onmessage = (ev) => {
        try {
          const d = JSON.parse(ev.data) as { apps: AppInfo[] };
          setApps(d.apps || []);
        } catch {
          /* malformed frame */
        }
      };
    } catch {
      /* unavailable */
    }

    // Current app is expanded by default: load its windows up front.
    for (const pid of expandedRef.current) loadWins(pid);

    // Keep expanded apps' window lists fresh (new/closed windows).
    const iv = setInterval(() => {
      for (const pid of expandedRef.current) loadWins(pid);
    }, 4000);

    return () => {
      es?.close();
      clearInterval(iv);
    };
  }, [loadWins]);

  // When the route's current app changes, expand it and load its windows —
  // parity with the old full-page load's default-expanded current app. The
  // current-app/window highlight already follows the route-derived PID/WINDOW_ID.
  useEffect(() => {
    if (!PID) return;
    setExpanded((prev) => (prev.has(PID) ? prev : new Set(prev).add(PID)));
    loadWins(PID);
  }, [PID, loadWins]);

  function toggleApp(pid: number) {
    setExpanded((prev) => {
      const next = new Set(prev);
      if (next.has(pid)) {
        next.delete(pid);
      } else {
        next.add(pid);
        loadWins(pid);
      }
      return next;
    });
  }

  return (
    <aside id="sidebar">
      <Link id="sb-home" className={onInfo ? "current" : ""} to="/info">
        Info
      </Link>

      {/* Terminal is a first-class "app": a real zsh session, listed alongside the
          real ones because it's an alternative to driving a GUI, not a setting.
          The same session is also stacked under every app page. */}
      <Link
        className={"sb-nav sb-term" + (onTerminal ? " current" : "")}
        to="/terminal"
      >
        <span className="sb-termicon">›_</span>
        <span className="sb-name">Terminal</span>
        <span className="sb-tag">zsh</span>
      </Link>

      {/* Full Screen — deferred: reserved slot only, no behavior yet. */}
      <div className="sb-nav disabled" aria-disabled="true">
        <span className="sb-name">Full Screen</span>
        <span className="soon">soon</span>
      </div>

      <div id="sb-apps">
        {apps.map((a) => {
          const isExpanded = expanded.has(a.pid);
          const wins = winsByPid.get(a.pid);
          return (
            <div className="sb-app" data-pid={a.pid} key={a.pid}>
              <div
                className={"sb-approw" + (a.pid === PID ? " current" : "")}
                onClick={() => navigate(`/app/${a.pid}`)}
              >
                <span
                  className="sb-caret"
                  onClick={(e) => {
                    e.stopPropagation();
                    toggleApp(a.pid);
                  }}
                >
                  {isExpanded ? "▾" : "▸"}
                </span>
                <img className="sb-icon" src={API.icon(a.pid)} alt="" />
                <span className="sb-name">{a.name}</span>
              </div>
              <div
                className="sb-wins"
                data-pid={a.pid}
                style={{ display: isExpanded ? "" : "none" }}
              >
                {isExpanded && wins !== undefined ? (
                  wins.length ? (
                    wins.map((w) => {
                      const isCur =
                        a.pid === PID &&
                        (WINDOW_ID !== null
                          ? w.window_id === WINDOW_ID
                          : !!w.focused);
                      const to =
                        w.window_id != null
                          ? `/app/${a.pid}.${w.window_id}`
                          : `/app/${a.pid}`;
                      return (
                        <Link
                          className={
                            "sb-win" +
                            (w.minimized ? " min" : "") +
                            (isCur ? " current" : "")
                          }
                          to={to}
                          key={w.window_id ?? "focused"}
                        >
                          {w.title || "(untitled window)"}
                        </Link>
                      );
                    })
                  ) : (
                    <div className="sb-empty">No windows</div>
                  )
                ) : null}
              </div>
            </div>
          );
        })}
      </div>

      <ClosedApps />

      <div id="sb-footer">
        <label className="sb-toggle">
          <span className="sb-toggle-label">Outline elements</span>
          <span className="sb-switch">
            <input
              type="checkbox"
              checked={debug}
              onChange={(e) => setDebug(e.target.checked)}
            />
            <span className="sb-slider" />
          </span>
        </label>
      </div>
    </aside>
  );
}
