// window.webuse — a tiny SDK for driving THE CURRENT app from injected JavaScript.
//
// Available on any /app/:handle page: browser agents (Claude in Chrome / Comet) can
// inject JS and call window.webuse.* to click, type, scroll, etc. Every method maps to
// the SAME Step messages the Driver UI already sends over the input WebSocket /
// POST /api/action, so behavior is identical to a real click / keypress in the page.
//
// Coordinate methods accept EITHER a ghost-element id (string, e.g. "e12") OR
// window-relative x,y numbers. Mutating methods return a Promise that resolves with the
// fresh post-action ghost DOM ({ window, truncated, nodes }).

import { API, getJSON, postJSON, type DomSnapshot, type GhostNode, type GhostWindow, type Step } from "./api";

// ---- Public surface -------------------------------------------------------

export interface Webuse {
  /** the app process id */
  readonly pid: number;
  /** "pid" or "pid.winid" */
  readonly handle: string;
  /** window id, or null for a bare-pid (whole-app) handle */
  readonly windowId: number | null;

  // Discovery
  /** fetch the fresh ghost DOM */
  dom(): Promise<DomSnapshot>;
  /** last-known nodes (sync; may be stale) */
  nodes(): GhostNode[];
  /** last-known nodes whose label/text/placeholder/value contains text (case-insensitive) */
  find(text: string): GhostNode[];

  // Pointer (first arg is an element id string, OR x with a second y number)
  click(idOrX: string | number, y?: number): Promise<DomSnapshot>;
  rightClick(idOrX: string | number, y?: number): Promise<DomSnapshot>;
  doubleClick(idOrX: string | number, y?: number): Promise<DomSnapshot>;
  hover(idOrX: string | number, y?: number): Promise<DomSnapshot>;
  /** AXPress a ghost element (menu items / buttons) in the background (no cursor move) */
  press(id: string): Promise<DomSnapshot>;
  /** wheel deltas at a point (defaults to the window center) */
  scroll(dx: number, dy: number, atX?: number, atY?: number): Promise<DomSnapshot>;
  drag(x1: number, y1: number, x2: number, y2: number): Promise<DomSnapshot>;

  // Keyboard
  /** type literal text */
  type(text: string): Promise<DomSnapshot>;
  /** one key tap; name is friendly ("Enter","n","Escape") or a raw KeyboardEvent.code */
  key(name: string): Promise<DomSnapshot>;
  /** key tap with modifiers ("cmd"|"command"|"meta"|"shift"|"ctrl"|"control"|"alt"|"option") */
  keyPress(name: string, modifiers?: string[]): Promise<DomSnapshot>;
  /** string form of keyPress, parts split on "+" (e.g. "cmd+shift+n") */
  hotkey(combo: string): Promise<DomSnapshot>;
  /** press-and-hold a key */
  keyDown(name: string, modifiers?: string[]): Promise<DomSnapshot>;
  /** release a key */
  keyUp(name: string, modifiers?: string[]): Promise<DomSnapshot>;

  // App
  /** raise the app to the foreground */
  activate(): Promise<DomSnapshot>;
}

export interface WebuseSdkContext {
  handle: string;
  pid: number;
  windowId: number | null;
  send: (step: Step) => void;
  getNodes: () => GhostNode[];
}

declare global {
  interface Window {
    webuse?: Webuse;
  }
}

// ---- Key & modifier resolution -------------------------------------------

const KEY_ALIASES: Record<string, string> = {
  enter: "Enter",
  return: "Enter",
  esc: "Escape",
  escape: "Escape",
  tab: "Tab",
  space: "Space",
  del: "Backspace",
  delete: "Backspace",
  backspace: "Backspace",
  up: "ArrowUp",
  down: "ArrowDown",
  left: "ArrowLeft",
  right: "ArrowRight",
  home: "Home",
  end: "End",
  pageup: "PageUp",
  pagedown: "PageDown",
};

// Friendly key name -> KeyboardEvent.code. Anything already matching a code is
// passed through unchanged.
function resolveKey(name: string): string {
  const lower = name.toLowerCase();
  if (KEY_ALIASES[lower]) return KEY_ALIASES[lower];
  if (/^[a-z]$/.test(lower)) return "Key" + lower.toUpperCase();
  if (/^[0-9]$/.test(name)) return "Digit" + name;
  if (/^f([1-9]|1[0-2])$/.test(lower)) return "F" + lower.slice(1);
  return name; // assume it is already a KeyboardEvent.code
}

interface ModFlags {
  shift: boolean;
  ctrl: boolean;
  alt: boolean;
  meta: boolean;
}

// A modifier-name array -> {shift,ctrl,alt,meta} booleans.
function modFlags(mods: string[] = []): ModFlags {
  const f: ModFlags = { shift: false, ctrl: false, alt: false, meta: false };
  for (const m of mods) {
    switch (m.toLowerCase()) {
      case "cmd":
      case "command":
      case "meta":
        f.meta = true;
        break;
      case "shift":
        f.shift = true;
        break;
      case "ctrl":
      case "control":
        f.ctrl = true;
        break;
      case "alt":
      case "option":
        f.alt = true;
        break;
    }
  }
  return f;
}

const SETTLE_MS = 130;
const sleep = (ms: number): Promise<void> => new Promise((r) => setTimeout(r, ms));

// ---- Installer ------------------------------------------------------------

// Install window.webuse for the given driver context. Returns an uninstaller that
// deletes window.webuse (only if it still points at this instance).
export function installWebuseSdk(ctx: WebuseSdkContext): () => void {
  const { handle, pid, windowId, send, getNodes } = ctx;
  let lastWindow: GhostWindow | null = null;

  async function fetchDom(): Promise<DomSnapshot> {
    const snap = await getJSON<DomSnapshot>(API.dom(handle));
    lastWindow = snap.window;
    return snap;
  }

  // Every mutating method: send the Step, let the app settle, then read the fresh DOM.
  async function run(step: Step): Promise<DomSnapshot> {
    send(step);
    await sleep(SETTLE_MS);
    return fetchDom();
  }

  async function windowGeom(): Promise<GhostWindow> {
    if (lastWindow) return lastWindow;
    return (await fetchDom()).window;
  }

  // First arg is a ghost-element id (string) OR an x number with a second y number.
  function pointer(action: string, idOrX: string | number, y?: number): Promise<DomSnapshot> {
    if (typeof idOrX === "string") return run({ action, id: idOrX });
    return run({ action, x: idOrX, y });
  }

  function keyStep(name: string, mods: string[] | undefined, down?: boolean): Step {
    const step: Step = { action: "key", code: resolveKey(name), ...modFlags(mods) };
    if (down !== undefined) step.down = down;
    return step;
  }

  const api: Webuse = {
    pid,
    handle,
    windowId,

    dom: () => fetchDom(),
    nodes: () => getNodes(),
    find(text: string): GhostNode[] {
      const q = text.toLowerCase();
      return getNodes().filter((n) => {
        const hay = [n.label, n.text, n.placeholder, n.value]
          .filter(Boolean)
          .join(" ")
          .toLowerCase();
        return hay.includes(q);
      });
    },

    click: (idOrX, y) => pointer("click", idOrX, y),
    rightClick: (idOrX, y) => pointer("rightclick", idOrX, y),
    doubleClick: (idOrX, y) => pointer("dblclick", idOrX, y),
    hover: (idOrX, y) => pointer("move", idOrX, y),
    press: (id) => run({ action: "press", id }),
    async scroll(dx, dy, atX, atY) {
      let x = atX;
      let y = atY;
      if (x === undefined || y === undefined) {
        const w = await windowGeom();
        x = x ?? w.w / 2;
        y = y ?? w.h / 2;
      }
      return run({ action: "scroll", x, y, dx, dy });
    },
    drag: (x1, y1, x2, y2) => run({ action: "drag", x: x1, y: y1, x2, y2 }),

    type: (text) => run({ action: "type", value: text }),
    key: (name) => run({ action: "key", code: resolveKey(name) }),
    keyPress: (name, modifiers) => run(keyStep(name, modifiers)),
    hotkey(combo) {
      const parts = combo.split("+").map((p) => p.trim()).filter(Boolean);
      const name = parts.pop() ?? "";
      return run(keyStep(name, parts));
    },
    keyDown: (name, modifiers) => run(keyStep(name, modifiers, true)),
    keyUp: (name, modifiers) => run(keyStep(name, modifiers, false)),

    // Raise the app via the dedicated endpoint (dispatch has no "activate" action;
    // routing that through /api/action would report a failed step and abort a batch).
    async activate() {
      await postJSON(API.activate, { pid, window_id: windowId });
      await sleep(SETTLE_MS);
      return fetchDom();
    },
  };

  window.webuse = api;

  return () => {
    if (window.webuse === api) delete window.webuse;
  };
}
