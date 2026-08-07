import { useCallback, useEffect, useRef, useState } from "react";
import { API, getJSON, postJSON, type TermInput, type TermSnapshot } from "../../api";

// Keys a web agent can't press but a shell needs constantly. Rendered as buttons
// so every one of them is reachable by a plain DOM click.
const KEYS: { key: string; label: string; title: string }[] = [
  { key: "Enter", label: "↵", title: "Return" },
  { key: "Tab", label: "⇥", title: "Tab — completion" },
  { key: "Ctrl-C", label: "^C", title: "Interrupt the running command" },
  { key: "Ctrl-D", label: "^D", title: "EOF — exit shell or REPL" },
  { key: "Ctrl-L", label: "clear", title: "Clear the screen" },
  { key: "Escape", label: "⎋", title: "Escape" },
  { key: "Up", label: "↑", title: "Previous command" },
  { key: "Down", label: "↓", title: "Next command" },
  { key: "Left", label: "←", title: "Left" },
  { key: "Right", label: "→", title: "Right" },
];

// Character-cell metrics of the screen font, measured once from a probe element,
// so the shell's grid can be sized to the panel instead of wrapping at a guess.
function cellSize(probe: HTMLElement): { w: number; h: number } {
  const r = probe.getBoundingClientRect();
  return { w: r.width / 10 || 8, h: r.height || 16 };
}

// How much of the transcript goes into the DOM by default.
//
// The server hands back up to 600 lines of scrollback, which at a wide terminal
// is ~50 KB of text — on its own more than an agent's whole page-extraction
// budget. Dumping that into the <pre> doesn't just crowd the page out: the
// NEWEST output is at the END, so a reader that truncates loses precisely the
// part that answers "what did my command print?", and everything after the <pre>
// (the input box, the Run button) disappears with it. So render the tail by
// default and let anything that wants the rest ask for it.
const TAIL_LINES = 120;
const TAIL_CHARS = 6000;

function tailOf(text: string): string {
  const lines = text.split("\n");
  let out = lines.slice(-TAIL_LINES).join("\n");
  if (out.length > TAIL_CHARS) {
    out = out.slice(out.length - TAIL_CHARS);
    out = out.slice(out.indexOf("\n") + 1); // start at a line boundary
  }
  return out;
}

// A live view of one server-side shell session. The PTY lives on the server, so
// several mounts of this component (the stacked panel and the /terminal route)
// are just views of the SAME shell — no state to hand off between them.
export default function Terminal({
  sid = "main",
  autoFocus = false,
}: {
  sid?: string;
  autoFocus?: boolean;
}) {
  const [snap, setSnap] = useState<TermSnapshot | null>(null);
  const [busy, setBusy] = useState(false);
  const [full, setFull] = useState(false);

  // What actually goes into the DOM (see TAIL_LINES), and how much is held back.
  const shown = snap ? (full ? snap.text : tailOf(snap.text)) : "Starting shell…";
  const hiddenLines = snap
    ? snap.text.split("\n").length - shown.split("\n").length
    : 0;
  const screenRef = useRef<HTMLPreElement>(null);
  const probeRef = useRef<HTMLSpanElement>(null);
  const inputRef = useRef<HTMLInputElement>(null);

  // Live output. SSE pushes a fresh snapshot whenever the shell writes anything.
  useEffect(() => {
    let dead = false;
    getJSON<TermSnapshot>(API.term(sid))
      .then((s) => !dead && setSnap(s))
      .catch(() => {});
    const es = new EventSource(API.termEvents(sid));
    es.onmessage = (ev) => {
      try {
        setSnap(JSON.parse(ev.data) as TermSnapshot);
      } catch {
        /* malformed frame */
      }
    };
    return () => {
      dead = true;
      es.close();
    };
  }, [sid]);

  // Keep the newest output in view.
  useEffect(() => {
    const el = screenRef.current;
    if (el) el.scrollTop = el.scrollHeight;
  }, [shown]);

  // Match the shell's grid to the rendered panel so wrapping is truthful.
  useEffect(() => {
    const el = screenRef.current;
    const probe = probeRef.current;
    if (!el || !probe) return;
    let timer: ReturnType<typeof setTimeout> | null = null;
    let last = "";
    const measure = () => {
      const { w, h } = cellSize(probe);
      const cols = Math.max(20, Math.floor((el.clientWidth - 16) / w));
      const rows = Math.max(5, Math.floor((el.clientHeight - 12) / h));
      const key = `${cols}x${rows}`;
      if (key === last) return;
      last = key;
      postJSON<TermSnapshot>(API.termResize(sid), { cols, rows })
        .then(setSnap)
        .catch(() => {});
    };
    const ro = new ResizeObserver(() => {
      if (timer) clearTimeout(timer);
      timer = setTimeout(measure, 150);
    });
    ro.observe(el);
    return () => {
      ro.disconnect();
      if (timer) clearTimeout(timer);
    };
  }, [sid]);

  const send = useCallback(
    async (body: TermInput) => {
      setBusy(true);
      try {
        setSnap(await postJSON<TermSnapshot>(API.termInput(sid), body));
      } catch {
        /* transient — SSE will resync */
      } finally {
        setBusy(false);
      }
    },
    [sid],
  );

  // Read the value out of the DOM rather than React state. Agent harnesses fill
  // inputs by assigning .value (sometimes without an event React can see), so a
  // controlled input would submit an empty string. Uncontrolled always reads what
  // is actually in the box, however it got there.
  function run() {
    const el = inputRef.current;
    const value = el?.value ?? "";
    if (el) el.value = "";
    void send({ value, enter: true });
    el?.focus();
  }

  const dead = snap && !snap.alive;

  return (
    <div className="term">
      {/* Above the screen, so it survives however much of the page a reader gets. */}
      {hiddenLines > 0 || full ? (
        <button className="term-more" onClick={() => setFull((v) => !v)}>
          {full
            ? "Show recent output only"
            : `Show ${hiddenLines} earlier lines of scrollback`}
        </button>
      ) : null}

      <pre className="term-screen" ref={screenRef} data-term={sid}>
        {shown}
      </pre>
      {/* Outside the <pre>: it is measurement scaffolding, and inside it the
          probe's digits land in the transcript any reader extracts. */}
      <span className="term-probe" ref={probeRef} aria-hidden="true">
        0000000000
      </span>

      {dead ? (
        <div className="term-dead">
          Shell exited ({snap?.exit_code ?? "?"}). Running a command starts a new one.
        </div>
      ) : snap?.busy ? (
        // Distinct from "output is arriving": a silent command still holds the
        // terminal, and this is what says so.
        <div className="term-busy">● running — ^C to interrupt</div>
      ) : null}

      <div className="term-bar">
        <input
          className="term-input"
          ref={inputRef}
          defaultValue=""
          autoFocus={autoFocus}
          spellCheck={false}
          autoComplete="off"
          aria-label="zsh command"
          placeholder="zsh command — e.g. osascript -e 'set volume output volume 30'"
          onKeyDown={(e) => {
            if (e.key === "Enter") {
              e.preventDefault();
              run();
            }
          }}
        />
        <button className="term-run" onClick={run} disabled={busy}>
          {busy ? "Running…" : "Run"}
        </button>
      </div>

      <div className="term-keys">
        {KEYS.map((k) => (
          <button
            key={k.key}
            className="keycap"
            title={k.title}
            onClick={() => void send({ key: k.key })}
          >
            {k.label}
          </button>
        ))}
      </div>
    </div>
  );
}
