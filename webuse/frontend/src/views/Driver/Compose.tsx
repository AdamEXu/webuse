import { useCallback, useEffect, useRef, useState } from "react";
import {
  API,
  getJSON,
  parseHandle,
  postJSON,
  type FocusInfo,
  type Step,
} from "../../api";

// The keys agents can't otherwise press. A web agent can usually only fill text
// inputs and click, so every non-text keystroke needs to exist as a button.
const KEYS: { label: string; title: string; step: Step }[] = [
  { label: "↵ Enter", title: "Return", step: { action: "key", code: "Enter" } },
  { label: "⇥ Tab", title: "Tab", step: { action: "key", code: "Tab" } },
  { label: "⎋ Esc", title: "Escape", step: { action: "key", code: "Escape" } },
  { label: "⌫", title: "Delete backwards", step: { action: "key", code: "Backspace" } },
  { label: "←", title: "Left", step: { action: "key", code: "ArrowLeft" } },
  { label: "→", title: "Right", step: { action: "key", code: "ArrowRight" } },
  { label: "↑", title: "Up", step: { action: "key", code: "ArrowUp" } },
  { label: "↓", title: "Down", step: { action: "key", code: "ArrowDown" } },
  { label: "⌘A", title: "Select all", step: { action: "key", code: "KeyA", meta: true } },
  { label: "⌘C", title: "Copy", step: { action: "key", code: "KeyC", meta: true } },
  { label: "⌘V", title: "Paste", step: { action: "key", code: "KeyV", meta: true } },
  { label: "⌘Z", title: "Undo", step: { action: "key", code: "KeyZ", meta: true } },
];

// Compose bar: a REAL editable input in webuse's own chrome (not a ghost), whose
// contents are delivered to the app as actual HID keystrokes on submit. Ghost text
// fields are deliberately non-editable mirrors, so this is the supported way to
// enter text — and the key palette covers everything typing alone can't express.
export default function Compose({ handle }: { handle: string }) {
  const { pid, windowId } = parseHandle(handle);
  const [focus, setFocus] = useState<FocusInfo | null>(null);
  const [busy, setBusy] = useState(false);
  const inputRef = useRef<HTMLInputElement>(null);

  // Poll where the app's keyboard focus sits, so the bar can say what typing
  // will hit instead of firing text blind.
  useEffect(() => {
    let dead = false;
    const read = () =>
      getJSON<{ focus: FocusInfo | null }>(API.focus(handle))
        .then((d) => !dead && setFocus(d.focus))
        .catch(() => {});
    read();
    const iv = setInterval(read, 1500);
    return () => {
      dead = true;
      clearInterval(iv);
    };
  }, [handle]);

  // Steps run server-side in order with a settle delay between them. `activate`
  // on the first step raises the app: browser focus is in THIS input, so without
  // it the keystrokes would land in the browser instead of the app.
  const run = useCallback(
    async (steps: Step[]) => {
      if (!steps.length) return;
      setBusy(true);
      try {
        await postJSON(API.action, {
          pid,
          window_id: windowId,
          actions: steps.map((s, i) => (i === 0 ? { ...s, activate: true } : s)),
        });
      } catch {
        /* transient — the ghost DOM resyncs over SSE regardless */
      } finally {
        setBusy(false);
        inputRef.current?.focus();
      }
    },
    [pid, windowId],
  );

  // Read straight from the DOM, not React state: agent harnesses fill inputs by
  // assigning .value, sometimes without an event React would see, and a controlled
  // input would then send nothing.
  function send(withEnter: boolean) {
    const el = inputRef.current;
    const value = el?.value ?? "";
    const steps: Step[] = [];
    if (value) steps.push({ action: "type", value });
    if (withEnter) steps.push({ action: "key", code: "Enter" });
    if (!steps.length) return;
    if (el) el.value = "";
    void run(steps);
  }

  const target = focus
    ? `${focus.label || focus.role.replace(/^AX/, "") || "focused element"}${
        focus.editable ? "" : " — not a text field"
      }`
    : "nothing focused — click a field in the app first";

  return (
    <div id="compose">
      <div className={"compose-target" + (focus?.editable ? " ok" : " warn")}>
        <span className="compose-target-label">Typing into:</span> {target}
      </div>
      <div className="compose-row">
        <input
          className="compose-input"
          ref={inputRef}
          defaultValue=""
          spellCheck={false}
          autoComplete="off"
          aria-label="Text to type into the app"
          placeholder="Text to type into the app — then Send, or Send ↵ to submit it"
          onKeyDown={(e) => {
            if (e.key === "Enter") {
              e.preventDefault();
              send(true);
            }
          }}
        />
        <button className="compose-send" disabled={busy} onClick={() => send(false)}>
          Send
        </button>
        <button
          className="compose-send primary"
          disabled={busy}
          title="Type the text, then press Return"
          onClick={() => send(true)}
        >
          Send ↵
        </button>
      </div>
      <div className="compose-keys">
        {KEYS.map((k) => (
          <button
            key={k.label}
            className="keycap"
            title={k.title}
            onClick={() => void run([k.step])}
          >
            {k.label}
          </button>
        ))}
      </div>
    </div>
  );
}
