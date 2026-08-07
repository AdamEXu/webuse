import { useCallback, useRef } from "react";
import { useParams } from "react-router-dom";
import { API, parseHandle, type Step } from "../../api";
import { useDriver, type DriverRefs } from "./useDriver";
import Menubar from "./Menubar";

// The hidden agent-note (agents read it): how to drive this page over
// POST /api/action. Ported from APP_PAGE, lightly adjusted for the SPA.
function agentNote(pid: number, windowId: number | null): string {
  const wid = windowId === null ? "null" : String(windowId);
  return (
    `This page is a live macOS application shown in the FOREGROUND: an MJPEG screen ` +
    `stream with an invisible ghost DOM of HTML elements positioned over it. The ghost ` +
    `DOM is for DISCOVERY only — read it to find controls and their labels/positions. ` +
    `ALL interaction is real HID input delivered to the app (the real cursor moves and ` +
    `the app is focused). Text-field ghosts are non-editable mirrors of the app's ` +
    `fields (their value/data-value reflects the app): setting .value or typing into ` +
    `them does nothing — to enter text, click the field, then use {"action":"type"}. ` +
    `Drive it via POST /api/action with {"pid":${pid},"window_id":${wid},...} — one ` +
    `step or "actions":[...] run strictly in order (window_id null targets the focused ` +
    `window). Point-addressed kinds take a ghost-element "id" (its center is the target) ` +
    `OR window-relative "x","y": click, rightclick, dblclick, move (hover), drag ` +
    `("x","y" -> "x2","y2"), scroll ("dx","dy" pixels). Keyboard kinds: type ("value": ` +
    `literal text) and key ("code": a KeyboardEvent.code like "Enter"/"KeyA", optional ` +
    `"shift"/"ctrl"/"alt"/"meta", "down": true/false or omit for a tap). The web menu bar ` +
    `still presses real menu items over Accessibility (action "press" with an "id"). ` +
    `Example: {"pid":${pid},"actions":[{"action":"click","id":"e12"},` +
    `{"action":"type","value":"hello"},{"action":"key","code":"Enter"}]}. ` +
    `On failure the response has "failed_step"; it always includes per-step "results" ` +
    `(each with "point":[x,y] where it landed) and "dom" — the fresh post-action ghost ` +
    `DOM ({window, truncated, nodes}) — so you act and observe in one round trip. ` +
    `Content updates live; re-read after acting.`
  );
}

// The per-app stage + engine. Remounted (key={handle}) on app/window switch so its
// stream, input/DOM sockets and SSE tear down and re-init cleanly — WITHOUT taking
// the persistent sidebar (in the Layout) down with it, which would blank the app
// list and reload every icon, making a client-side nav look like a full reload.
function Stage({ handle }: { handle: string }) {
  const screen = useRef<HTMLImageElement>(null);
  const overlay = useRef<HTMLDivElement>(null);
  const off = useRef<HTMLDivElement>(null);
  const stage = useRef<HTMLDivElement>(null);
  const stagewrap = useRef<HTMLDivElement>(null);
  const stagebox = useRef<HTMLDivElement>(null);
  const banner = useRef<HTMLDivElement>(null);
  const status = useRef<HTMLDivElement>(null);

  const refs = useRef<DriverRefs>({
    screen,
    overlay,
    off,
    stage,
    stagewrap,
    stagebox,
    banner,
    status,
  }).current;

  const actRef = useDriver(handle, refs);
  const act = useCallback((p: Step) => actRef.current(p), [actRef]);

  // banner/status/kb (position:fixed) and #offscreen (off-screen absolute) are out
  // of flow, so living inside #content here doesn't disturb the #main flex column.
  return (
    <>
      <div id="banner" ref={banner} />
      <div id="status" ref={status} />
      <div id="kb" onClick={() => stage.current?.blur()}>
        ⌨ keyboard captured — click to release
      </div>
      <main id="main">
        <Menubar handle={handle} act={act} />
        <div id="stagewrap" ref={stagewrap}>
          <div id="stagebox" ref={stagebox}>
            <div id="stage" tabIndex={0} ref={stage}>
              <img
                id="screen"
                ref={screen}
                src={API.stream(handle)}
                alt="live app screen"
                draggable={false}
              />
              <div id="overlay" ref={overlay} />
            </div>
          </div>
        </div>
      </main>
      <div id="offscreen" ref={off} />
    </>
  );
}

// Route component (rendered inside the Layout's #content): a persistent hidden
// agent-note plus the <Stage> keyed on handle. Switching apps/windows swaps the
// stream in place instead of blanking and rebuilding the whole page.
export default function Driver() {
  const { handle = "" } = useParams();
  const { pid, windowId } = parseHandle(handle);
  return (
    <>
      <div id="agent-note">{agentNote(pid, windowId)}</div>
      <Stage key={handle} handle={handle} />
    </>
  );
}
