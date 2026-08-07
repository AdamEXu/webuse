// Typed client for the webuse backend. Same-origin in production; in dev the
// Vite proxy forwards these paths to the Python server (see vite.config.ts).

// ---- API shapes ----------------------------------------------------------

export interface AppInfo {
  pid: number;
  name: string;
  bundle_id: string | null;
}

export interface InstalledApp {
  name: string;
  bundle_id: string | null;
  path: string;
  running: boolean;
  pid: number | null;
}

export interface WindowInfo {
  window_id: number | null;
  title: string;
  focused: boolean;
  minimized: boolean;
}

export interface Permissions {
  trusted: boolean;
  screen_recording: boolean;
}

export interface MenuItem {
  title?: string;
  children?: MenuItem[];
  id?: string;
  shortcut?: string;
  disabled?: boolean;
  checked?: boolean;
  separator?: boolean;
}

export interface GhostWindow {
  app: string;
  title: string;
  x: number;
  y: number;
  w: number;
  h: number;
}

export interface GhostNode {
  id: string;
  x: number;
  y: number;
  w: number;
  h: number;
  label: string;
  placeholder: string;
  help: string;
  disabled: boolean;
  offscreen: boolean;
  kind: string; // "text" | "press" | "static"
  role: string; // raw AXRole (e.g. "AXSlider", "AXTab") — disambiguates within a kind
  tag: string; // div (text mirror) | input (checkbox/radio) | button | a | span
  type?: string; // input type (text/checkbox/radio)
  text?: string;
  value?: string;
  checked?: boolean;
}

export interface DomSnapshot {
  window: GhostWindow;
  truncated: boolean;
  nodes: GhostNode[];
}

// A single input/action step sent over the WS input channel or POST /api/action.
export interface Step {
  action: string;
  id?: string;
  value?: string;
  x?: number;
  y?: number;
  x2?: number;
  y2?: number;
  dx?: number;
  dy?: number;
  code?: string;
  down?: boolean;
  shift?: boolean;
  ctrl?: boolean;
  alt?: boolean;
  meta?: boolean;
  activate?: boolean;
  pid?: number;
  window_id?: number | null;
}

export interface OpenResult {
  ok: boolean;
  pid?: number;
  [k: string]: unknown;
}

// Where the app's keyboard focus sits — what typed text will land in.
export interface FocusInfo {
  role: string;
  label: string;
  value: string | null;
  editable: boolean;
}

// A terminal session's rendered state. `text` is scrollback + viewport (what a
// human sees after scrolling up); `screen` is the viewport alone.
export interface TermSnapshot {
  id: string;
  text: string;
  screen: string[];
  cursor: [number, number];
  cols: number;
  rows: number;
  version: number;
  idle_ms: number;
  busy: boolean; // a command still owns the terminal (not just "output went quiet")
  alive: boolean;
  exit_code: number | null;
  settled?: boolean;
}

export interface TermInput {
  value?: string;
  key?: string;
  enter?: boolean;
  wait?: boolean;
  quiet_ms?: number;
  timeout_ms?: number;
}

// ---- endpoint helpers ----------------------------------------------------

export const API = {
  apps: (installed = false) => `/api/apps${installed ? "?installed=1" : ""}`,
  appsEvents: "/api/apps/events",
  appsOpen: "/api/apps/open",
  permissions: "/api/permissions",
  permissionsRequest: "/api/permissions/request",
  activate: "/api/activate",
  action: "/api/action",
  term: (sid: string) => `/api/term/${sid}`,
  termInput: (sid: string) => `/api/term/${sid}/input`,
  termResize: (sid: string) => `/api/term/${sid}/resize`,
  termEvents: (sid: string) => `/api/term/${sid}/events`,
  focus: (handle: string) => `/app/${handle}/focus`,
  windows: (pid: number) => `/app/${pid}/windows`,
  icon: (pid: number) => `/app/${pid}/icon`,
  menu: (handle: string) => `/app/${handle}/menu`,
  dom: (handle: string) => `/app/${handle}/dom`,
  events: (handle: string) => `/app/${handle}/events`,
  stream: (handle: string) => `/app/${handle}/stream`,
  streamWs: (handle: string) => `/app/${handle}/stream_ws`,
  inputWs: (handle: string) => `/app/${handle}/ws`,
} as const;

export const SETTINGS_URL =
  "x-apple.systempreferences:com.apple.preference.security?Privacy_";

// "pid" or "pid.winid" -> {pid, windowId}. windowId is null for a bare pid.
export function parseHandle(handle: string): { pid: number; windowId: number | null } {
  const [pidStr, winStr] = handle.split(".");
  const pid = Number.parseInt(pidStr, 10);
  const windowId = winStr !== undefined ? Number.parseInt(winStr, 10) : null;
  return { pid, windowId };
}

export function formatHandle(pid: number, windowId: number | null): string {
  return windowId !== null ? `${pid}.${windowId}` : `${pid}`;
}

export async function getJSON<T>(url: string): Promise<T> {
  const r = await fetch(url);
  if (!r.ok) throw new Error(`${url} -> ${r.status}`);
  return (await r.json()) as T;
}

export async function postJSON<T>(url: string, body: unknown): Promise<T> {
  const r = await fetch(url, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  return (await r.json()) as T;
}

// Build the ws:// or wss:// URL for a same-origin WebSocket path.
export function wsURL(path: string): string {
  const proto = location.protocol === "https:" ? "wss" : "ws";
  return `${proto}://${location.host}${path}`;
}
