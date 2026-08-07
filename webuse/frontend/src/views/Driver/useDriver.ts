import { useEffect, useRef, type RefObject } from "react";
import {
  API,
  parseHandle,
  wsURL,
  type DomSnapshot,
  type GhostNode,
  type Step,
} from "../../api";
import { installWebuseSdk } from "../../sdk";
import { setStyle, sync } from "./ghost";

export interface DriverRefs {
  screen: RefObject<HTMLImageElement>;
  overlay: RefObject<HTMLDivElement>;
  off: RefObject<HTMLDivElement>;
  stage: RefObject<HTMLDivElement>;
  stagewrap: RefObject<HTMLDivElement>;
  stagebox: RefObject<HTMLDivElement>;
  banner: RefObject<HTMLDivElement>;
  status: RefObject<HTMLDivElement>;
}

// The Driver engine: stream transport, ghost-DOM discovery, real-HID input and
// keyboard capture — ported 1:1 from the APP_PAGE <script>. Returns a stable ref
// to `act` so the web menu bar can press real menu items and refresh the DOM.
export function useDriver(handle: string, refs: DriverRefs) {
  const actRef = useRef<(payload: Step) => void>(() => {});

  useEffect(() => {
    const { pid: PID, windowId: WINDOW_ID } = parseHandle(handle);
    const screenEl = refs.screen.current;
    const overlay = refs.overlay.current;
    const off = refs.off.current;
    const stage = refs.stage.current;
    const stagewrap = refs.stagewrap.current;
    const stagebox = refs.stagebox.current;
    const banner = refs.banner.current;
    const statusEl = refs.status.current;
    if (
      !screenEl || !overlay || !off || !stage || !stagewrap || !stagebox ||
      !banner || !statusEl
    ) {
      return;
    }

    let disposed = false;

    // ---- Responsive feed scaling: shrink the native-sized stage (window points)
    // to fit the area left of the sidebar / below the menu bar, preserving aspect,
    // never enlarging (scale <= 1). The whole #stage is scaled with one CSS
    // transform, so the ghost overlay stays pixel-aligned with the streamed frame.
    let winW = 0, winH = 0, scale = 1;
    function applyScale() {
      if (!winW || !winH) return;
      const availW = stagewrap!.clientWidth, availH = stagewrap!.clientHeight;
      if (availW < 1 || availH < 1) return;
      scale = Math.min(availW / winW, availH / winH, 1);
      setStyle(stage!, "width", winW + "px");
      setStyle(stage!, "height", winH + "px");
      setStyle(stage!, "transform", "scale(" + scale + ")");
      setStyle(stagebox!, "width", winW * scale + "px");
      setStyle(stagebox!, "height", winH * scale + "px");
    }
    window.addEventListener("resize", applyScale);

    // ---- Low-latency input over a WebSocket: one persistent connection carries
    // every mouse/keyboard step. Falls back to POST until the socket is open. ----
    let ws: WebSocket | null = null;
    let wsOpen = false;
    function connectWS() {
      if (disposed) return;
      ws = new WebSocket(wsURL(API.inputWs(handle)));
      ws.onopen = () => {
        wsOpen = true;
      };
      ws.onclose = () => {
        wsOpen = false;
        if (!disposed) setTimeout(connectWS, 1000);
      };
      ws.onerror = () => {
        try {
          ws?.close();
        } catch {
          /* already closing */
        }
      };
    }
    connectWS();

    function send(payload: Step) {
      payload.pid = PID;
      if (WINDOW_ID !== null && payload.window_id === undefined)
        payload.window_id = WINDOW_ID;
      if (wsOpen && ws && ws.readyState === 1) {
        try {
          ws.send(JSON.stringify(payload));
          return;
        } catch {
          /* fall through to POST */
        }
      }
      fetch(API.action, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      }).catch(() => {});
    }

    let refreshTimer: ReturnType<typeof setTimeout> | null = null;
    function refreshSoon() {
      if (refreshTimer) clearTimeout(refreshTimer);
      refreshTimer = setTimeout(refresh, 250);
    }
    // Menu bar drives real menu items over Accessibility; then re-read the DOM.
    function act(payload: Step) {
      send(payload);
      refreshSoon();
    }
    actRef.current = act;

    // Raise the app to the foreground (one app at a time) so HID reaches it.
    function activateApp() {
      fetch(API.activate, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ pid: PID, window_id: WINDOW_ID }),
      }).catch(() => {});
    }

    // ---- Scroll: a wheel over the stage posts a real HID scroll at that point ----
    let wheelAccum = { dx: 0, dy: 0 };
    let wheelTimer: ReturnType<typeof setTimeout> | null = null;
    let wheelPt: { x: number; y: number } | null = null;
    function onWheel(ev: WheelEvent) {
      ev.preventDefault(); // don't scroll the browser page
      wheelPt = rel(ev);
      wheelAccum.dx += ev.deltaX;
      wheelAccum.dy += ev.deltaY;
      if (!wheelTimer) wheelTimer = setTimeout(flushWheel, 55);
    }
    function flushWheel() {
      wheelTimer = null;
      const dx = wheelAccum.dx, dy = wheelAccum.dy;
      wheelAccum = { dx: 0, dy: 0 };
      if (!wheelPt || (!dx && !dy)) return;
      send({ action: "scroll", x: wheelPt.x, y: wheelPt.y, dx, dy });
      refreshSoon();
    }
    stage.addEventListener("wheel", onWheel, { passive: false });

    // ---- Ghost DOM render ----
    const lastNode = new Map<string, string>(); // id -> JSON last applied
    let lastNodes: GhostNode[] = []; // live cache for the injected window.webuse SDK
    function render(d: DomSnapshot) {
      lastNodes = d.nodes;
      const w = d.window;
      const title = (w.title ? w.title + " — " : "") + w.app;
      if (document.title !== title) document.title = title;
      const size = w.w + "x" + w.h;
      if (lastSize && size !== lastSize) restartStream(); // restart at new resolution
      lastSize = size;
      // ONE source of truth for the current window: winW/winH size #stage and the
      // uniform scale; #screen fills #stage (object-fit:contain), so it can't stretch.
      winW = w.w;
      winH = w.h;
      applyScale();
      sync(overlay!, d.nodes.filter((n) => !n.offscreen), lastNode);
      sync(off!, d.nodes.filter((n) => n.offscreen), lastNode);
    }
    async function refresh() {
      try {
        const r = await fetch(API.dom(handle));
        if (r.ok) render((await r.json()) as DomSnapshot);
      } catch {
        /* transient */
      }
    }

    // ---- Mouse: forward every pointer event on the stage as real HID input.
    // Coords are window-relative (the server adds the window origin -> global). ----
    function rel(ev: { clientX: number; clientY: number }) {
      // Map from #screen's LIVE rect to true window points via fractions, so this
      // is scale-invariant and resize-robust.
      const r = screenEl!.getBoundingClientRect();
      const fx = r.width ? (ev.clientX - r.left) / r.width : 0;
      const fy = r.height ? (ev.clientY - r.top) / r.height : 0;
      return { x: fx * winW, y: fy * winH };
    }
    let hoverAt = 0; // last hover post (throttle)
    let press: { x: number; y: number } | null = null; // where left went down
    let dragged = false; // moved past the drag threshold while pressed
    let swallowClick = false; // suppress the click that trails a completed drag

    function onMouseMove(ev: MouseEvent) {
      const p = rel(ev);
      if (press) {
        if (Math.hypot(p.x - press.x, p.y - press.y) > 4) dragged = true;
        return;
      }
      const now = performance.now();
      if (now - hoverAt < 16) return; // throttle hover to ~60fps
      hoverAt = now;
      send({ action: "move", x: p.x, y: p.y });
    }
    function onMouseDown(ev: MouseEvent) {
      if (ev.button === 0) {
        press = rel(ev);
        dragged = false;
        swallowClick = false;
      }
    }
    function onMouseUp(ev: MouseEvent) {
      if (ev.button !== 0 || !press) return;
      if (dragged) {
        // press-drag-release becomes one real drag gesture
        const p = rel(ev);
        send({ action: "drag", x: press.x, y: press.y, x2: p.x, y2: p.y });
        swallowClick = true;
        refreshSoon();
      }
      press = null;
      dragged = false;
    }
    function onClick(ev: MouseEvent) {
      if (swallowClick) {
        swallowClick = false;
        return;
      }
      if (ev.detail > 1) return; // dblclick handles doubles
      const p = rel(ev);
      send({ action: "click", x: p.x, y: p.y });
      refreshSoon();
    }
    function onDblClick(ev: MouseEvent) {
      const p = rel(ev);
      send({ action: "dblclick", x: p.x, y: p.y });
      refreshSoon();
    }
    function onContextMenu(ev: MouseEvent) {
      ev.preventDefault();
      const p = rel(ev);
      send({ action: "rightclick", x: p.x, y: p.y });
      refreshSoon();
    }
    stage.addEventListener("mousemove", onMouseMove);
    stage.addEventListener("mousedown", onMouseDown);
    stage.addEventListener("mouseup", onMouseUp);
    stage.addEventListener("click", onClick);
    stage.addEventListener("dblclick", onDblClick);
    stage.addEventListener("contextmenu", onContextMenu);

    // ---- Node-clickable ghosts. Some harnesses (e.g. TinyFish) can only click a
    // real DOM node, not a bare coordinate on the stage. Ghost elements are
    // pointer-events:auto, so they receive the click; here we forward a real HID
    // click at the cursor position if it lands INSIDE the element (a real pointer),
    // else at the element's center (a synthetic node click carries no useful point).
    // The overlay container is pointer-events:none, so empty-space clicks still fall
    // through to the stage handlers above. ----
    function onGhostDown(ev: MouseEvent) {
      const el = (ev.target as HTMLElement | null)?.closest<HTMLElement>(".ghost");
      if (!el) return;
      // Keyboard capture is gated on the STAGE having focus (that lights the green
      // "captured" badge and routes keys to the app). A ghost <button>/<input>/<a>
      // would otherwise take focus itself, so the badge never turns on and typing
      // goes nowhere. preventDefault stops the ghost grabbing focus; then we focus
      // the stage explicitly. The real HID click (onGhostClick) still drives the
      // native control, so the native field ends up focused in the app.
      ev.preventDefault();
      stage!.focus({ preventScroll: true });
    }
    function onGhostClick(ev: MouseEvent) {
      const el = (ev.target as HTMLElement | null)?.closest<HTMLElement>(".ghost");
      if (!el || el.tagName === "SPAN") return; // static text isn't actionable
      ev.preventDefault(); // no anchor nav / default action — the app is driven, not the ghost
      ev.stopPropagation(); // the stage's coordinate onClick must not also fire
      if (swallowClick) {
        swallowClick = false;
        return;
      } // trailing click of a completed drag
      const r = el.getBoundingClientRect();
      const inside =
        ev.clientX >= r.left &&
        ev.clientX <= r.right &&
        ev.clientY >= r.top &&
        ev.clientY <= r.bottom;
      const p = inside
        ? rel(ev) // real cursor over the element -> its exact position
        : { x: el.offsetLeft + el.offsetWidth / 2, y: el.offsetTop + el.offsetHeight / 2 }; // synthetic click -> element center (already window points)
      send({ action: "click", x: p.x, y: p.y });
      refreshSoon();
    }
    overlay.addEventListener("mousedown", onGhostDown);
    overlay.addEventListener("click", onGhostClick);

    // ---- Belt-and-suspenders: text ghosts are non-editable divs, so nothing
    // should ever edit-target a ghost — but if any residual editable surface is
    // hit (an IME, execCommand, a synthetic paste), reroute the text to real HID
    // instead of letting it strand in the mirror. Capture phase, gated on the
    // target being a .ghost. No double delivery: a trusted stage keydown never
    // reaches these (its target is #stage, not a ghost), and a ghost keydown
    // stops propagating so the stage's bubbling handler can't also forward it.
    function ghostTarget(ev: Event): HTMLElement | null {
      return (ev.target as HTMLElement | null)?.closest<HTMLElement>(".ghost") ?? null;
    }
    function onGhostBeforeInput(ev: InputEvent) {
      if (!ghostTarget(ev)) return;
      ev.preventDefault();
      stage!.focus({ preventScroll: true });
      if (ev.inputType.startsWith("delete")) {
        send({ action: "key", code: "Backspace" });
      } else if (ev.data) {
        send({ action: "type", value: ev.data });
      }
      refreshSoon();
    }
    function onGhostPaste(ev: ClipboardEvent) {
      if (!ghostTarget(ev)) return;
      ev.preventDefault();
      stage!.focus({ preventScroll: true });
      const text = ev.clipboardData?.getData("text") || "";
      if (text) {
        send({ action: "type", value: text });
        refreshSoon();
      }
    }
    function onGhostKeyDown(ev: KeyboardEvent) {
      if (!ghostTarget(ev)) return; // stage-focused typing is handled by onKeyDown
      ev.preventDefault();
      ev.stopPropagation(); // don't let it bubble into the stage handler too
      stage!.focus({ preventScroll: true }); // subsequent keys take the normal path
      if (!isMod(ev.code)) send({ action: "key", code: ev.code });
    }
    overlay.addEventListener("beforeinput", onGhostBeforeInput, true);
    overlay.addEventListener("paste", onGhostPaste, true);
    overlay.addEventListener("keydown", onGhostKeyDown, true);

    // ---- Keyboard: forward keys as real HID, only while the stage has focus. ----
    const held = new Set<string>();
    const MOD: Record<string, string> = {
      shiftKey: "ShiftLeft",
      ctrlKey: "ControlLeft",
      altKey: "AltLeft",
      metaKey: "MetaLeft",
    };
    const modDown: Record<string, boolean> = {
      shiftKey: false,
      ctrlKey: false,
      altKey: false,
      metaKey: false,
    };
    const isMod = (code: string) =>
      /^(Shift|Control|Alt|Meta)(Left|Right)$/.test(code);
    function syncMods(ev: KeyboardEvent) {
      for (const k in MOD) {
        const cur = ev[k as keyof KeyboardEvent] as unknown as boolean;
        if (cur !== modDown[k]) {
          send({ action: "key", code: MOD[k], down: cur });
          modDown[k] = cur;
        }
      }
    }
    function releaseHeld() {
      for (const code of held) send({ action: "key", code, down: false });
      held.clear();
      for (const k in MOD) {
        if (modDown[k]) {
          send({ action: "key", code: MOD[k], down: false });
          modDown[k] = false;
        }
      }
    }
    function onFocus() {
      document.body.classList.add("capturing");
      activateApp(); // raise the app so keystrokes land there
    }
    function onBlur() {
      document.body.classList.remove("capturing");
      releaseHeld();
    }
    function onKeyDown(ev: KeyboardEvent) {
      ev.preventDefault(); // the app, not the browser, handles it
      syncMods(ev); // correct modifier state first (self-heals lost keyups)
      if (isMod(ev.code)) return; // the modifier itself is delivered by syncMods
      held.add(ev.code);
      send({ action: "key", code: ev.code, down: true });
    }
    function onKeyUp(ev: KeyboardEvent) {
      ev.preventDefault();
      if (isMod(ev.code)) {
        syncMods(ev);
        return;
      }
      held.delete(ev.code);
      send({ action: "key", code: ev.code, down: false });
      syncMods(ev);
    }
    stage.addEventListener("focus", onFocus);
    stage.addEventListener("blur", onBlur);
    window.addEventListener("blur", releaseHeld);
    function onVisibility() {
      if (document.hidden) releaseHeld();
    }
    document.addEventListener("visibilitychange", onVisibility);
    stage.addEventListener("keydown", onKeyDown);
    stage.addEventListener("keyup", onKeyUp);

    // ---- Stream transport: start on MJPEG (the <img> src), upgrade to the
    // lower-latency binary-JPEG WebSocket when it connects; keep MJPEG on failure. ----
    let lastSize: string | null = null;
    let statusTimer: ReturnType<typeof setTimeout> | null = null;
    let streamWS: WebSocket | null = null;
    let wsFrames = false;
    let curBlobUrl: string | null = null;
    let pendingReq = false;
    function wsActive() {
      return !!streamWS && streamWS.readyState === 1 && wsFrames;
    }
    function connectStreamWS() {
      if (disposed) return;
      let sock: WebSocket;
      try {
        sock = new WebSocket(wsURL(API.streamWs(handle)));
      } catch {
        return;
      }
      sock.binaryType = "blob";
      streamWS = sock;
      sock.onopen = () => {
        pendingReq = true;
        try {
          sock.send("n");
        } catch {
          /* closing */
        }
      }; // pull the first frame
      sock.onmessage = (ev: MessageEvent) => {
        pendingReq = false; // the frame we asked for arrived
        wsFrames = true;
        const url = URL.createObjectURL(ev.data as Blob);
        const old = curBlobUrl;
        curBlobUrl = url;
        screenEl!.src = url; // replaces the MJPEG connection on the first frame
        statusEl!.style.display = "none";
        if (old) setTimeout(() => URL.revokeObjectURL(old), 200);
      };
      sock.onclose = () => {
        streamWS = null;
        if (wsFrames) {
          wsFrames = false;
          restartStream();
        } // fall back to MJPEG
        if (!disposed) setTimeout(connectStreamWS, 1500);
      };
      sock.onerror = () => {
        try {
          sock.close();
        } catch {
          /* already closing */
        }
      };
    }
    function restartStream() {
      if (wsActive()) return; // the WebSocket carries frames + handles resize itself
      statusEl!.textContent = "Reconnecting to app stream…";
      statusEl!.style.display = "block";
      if (curBlobUrl) {
        URL.revokeObjectURL(curBlobUrl);
        curBlobUrl = null;
      }
      screenEl!.src = `${API.stream(handle)}?ts=${Date.now()}`;
      if (statusTimer) clearTimeout(statusTimer);
      statusTimer = setTimeout(() => {
        statusEl!.style.display = "none";
      }, 2000);
    }
    // Client-paced pull: request the next frame only AFTER this one has rendered.
    function pullNext() {
      if (pendingReq) return; // never more than one outstanding request
      if (streamWS && streamWS.readyState === 1 && wsFrames) {
        pendingReq = true;
        try {
          streamWS.send("n");
        } catch {
          /* closing */
        }
      }
    }
    function onScreenLoad() {
      statusEl!.style.display = "none";
      pullNext();
    }
    function onScreenError() {
      if (wsFrames) {
        pullNext();
        return;
      } // skip a bad WS frame, keep the pull going
      setTimeout(restartStream, 500); // MJPEG failure -> reconnect
    }
    screenEl.addEventListener("load", onScreenLoad);
    screenEl.addEventListener("error", onScreenError);
    connectStreamWS();

    // Expose window.webuse so injected JS (browser agents) can drive this app —
    // its steps funnel through the SAME send()/DOM path as real UI input.
    const uninstallSdk = installWebuseSdk({
      handle,
      pid: PID,
      windowId: WINDOW_ID,
      send,
      getNodes: () => lastNodes,
    });

    // On load: raise the app, initial paint, then live DOM over SSE.
    activateApp();
    refresh();

    const es = new EventSource(API.events(handle)); // auto-reconnects on error
    es.onmessage = (ev) => {
      document.body.classList.remove("gone");
      render(JSON.parse(ev.data) as DomSnapshot);
    };
    es.addEventListener("gone", (ev) => {
      const data = JSON.parse((ev as MessageEvent).data) as { error?: string };
      banner.textContent = "App unavailable: " + (data.error || "window gone");
      document.body.classList.add("gone");
    });

    return () => {
      disposed = true;
      uninstallSdk();
      window.removeEventListener("resize", applyScale);
      window.removeEventListener("blur", releaseHeld);
      document.removeEventListener("visibilitychange", onVisibility);
      stage.removeEventListener("wheel", onWheel);
      stage.removeEventListener("mousemove", onMouseMove);
      stage.removeEventListener("mousedown", onMouseDown);
      stage.removeEventListener("mouseup", onMouseUp);
      stage.removeEventListener("click", onClick);
      stage.removeEventListener("dblclick", onDblClick);
      stage.removeEventListener("contextmenu", onContextMenu);
      overlay.removeEventListener("mousedown", onGhostDown);
      overlay.removeEventListener("click", onGhostClick);
      overlay.removeEventListener("beforeinput", onGhostBeforeInput, true);
      overlay.removeEventListener("paste", onGhostPaste, true);
      overlay.removeEventListener("keydown", onGhostKeyDown, true);
      stage.removeEventListener("focus", onFocus);
      stage.removeEventListener("blur", onBlur);
      stage.removeEventListener("keydown", onKeyDown);
      stage.removeEventListener("keyup", onKeyUp);
      screenEl.removeEventListener("load", onScreenLoad);
      screenEl.removeEventListener("error", onScreenError);
      if (refreshTimer) clearTimeout(refreshTimer);
      if (wheelTimer) clearTimeout(wheelTimer);
      if (statusTimer) clearTimeout(statusTimer);
      if (curBlobUrl) URL.revokeObjectURL(curBlobUrl);
      try {
        es.close();
      } catch {
        /* noop */
      }
      try {
        ws?.close();
      } catch {
        /* noop */
      }
      try {
        streamWS?.close();
      } catch {
        /* noop */
      }
      document.body.classList.remove("capturing", "gone");
    };
  }, [handle, refs]);

  return actRef;
}
