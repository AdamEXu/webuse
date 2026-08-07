import { useCallback, useEffect, useRef, useState } from "react";
import Terminal from "../Terminal/Terminal";

const OPEN_KEY = "webuse:term:open";
const HEIGHT_KEY = "webuse:term:height";
const DEFAULT_HEIGHT = 240;
const MIN_HEIGHT = 120;

function readStored(key: string, fallback: number): number {
  const n = Number.parseInt(localStorage.getItem(key) ?? "", 10);
  return Number.isFinite(n) ? n : fallback;
}

// The terminal stacked under the live app view, so the GUI and a real shell are
// visible at the same time. It shows the same server-side session as the
// /terminal route — this is a second view of one shell, not a second shell.
export default function TerminalPanel() {
  // Closed until asked for: the app view is the point of the page, and the shell
  // is a tool you reach for. Once toggled, the choice sticks across pages/reloads.
  const [open, setOpen] = useState(
    () => (localStorage.getItem(OPEN_KEY) ?? "0") === "1",
  );
  const [height, setHeight] = useState(() => readStored(HEIGHT_KEY, DEFAULT_HEIGHT));
  const drag = useRef<{ y: number; h: number } | null>(null);

  useEffect(() => {
    localStorage.setItem(OPEN_KEY, open ? "1" : "0");
  }, [open]);
  useEffect(() => {
    localStorage.setItem(HEIGHT_KEY, String(height));
  }, [height]);

  // Drag the top edge to trade space with the app stage above. The stage rescales
  // itself: useDriver observes #stagewrap, so no resize event is needed here.
  const onDown = useCallback(
    (e: React.MouseEvent) => {
      e.preventDefault();
      drag.current = { y: e.clientY, h: height };
      const onMove = (ev: MouseEvent) => {
        if (!drag.current) return;
        const next = drag.current.h + (drag.current.y - ev.clientY);
        setHeight(Math.max(MIN_HEIGHT, Math.min(next, window.innerHeight - 200)));
      };
      const onUp = () => {
        drag.current = null;
        window.removeEventListener("mousemove", onMove);
        window.removeEventListener("mouseup", onUp);
      };
      window.addEventListener("mousemove", onMove);
      window.addEventListener("mouseup", onUp);
    },
    [height],
  );

  return (
    <section id="termpanel" className={open ? "open" : "closed"}>
      {open ? <div className="term-grip" onMouseDown={onDown} /> : null}
      <header className="term-panel-head">
        <button className="term-toggle" onClick={() => setOpen((v) => !v)}>
          {open ? "▾" : "▸"} Terminal
        </button>
        <span className="term-panel-hint">
          real zsh on this Mac — use it instead of clicking for anything a command can do
        </span>
      </header>
      {open ? (
        <div className="term-panel-body" style={{ height }}>
          <Terminal sid="main" />
        </div>
      ) : null}
    </section>
  );
}
