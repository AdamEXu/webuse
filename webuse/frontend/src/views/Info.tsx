// ---------------------------------------------------------------------------
// Info — the operational doc for driving webuse. Written for automated browser
// agents: how to use it, nothing else. Self-contained Tailwind utilities only
// (preflight is disabled, so sizes/margins/colors are set explicitly).
// ---------------------------------------------------------------------------

function Code({ children }: { children: string }) {
  return (
    <code className="rounded bg-neutral-100 px-1.5 py-0.5 font-mono text-[12px] text-neutral-800 dark:bg-neutral-800 dark:text-neutral-200">
      {children}
    </code>
  );
}

function H2({ children }: { children: React.ReactNode }) {
  return (
    <h2 className="mb-3 mt-10 text-[19px] font-semibold text-neutral-900 dark:text-neutral-100">
      {children}
    </h2>
  );
}

export default function Info() {
  return (
    <div className="min-h-0 min-w-0 flex-1 overflow-y-auto bg-white text-[14px] leading-relaxed text-neutral-700 dark:bg-neutral-900 dark:text-neutral-300">
      <div className="mx-auto max-w-3xl px-6 py-10">
        <h1 className="m-0 text-[26px] font-bold tracking-tight text-neutral-900 dark:text-neutral-50">
          webuse
        </h1>

        <H2>Driving it</H2>
        <div className="my-3 rounded-lg border border-sky-200 bg-sky-50 px-4 py-3 dark:border-sky-900 dark:bg-sky-950/40">
          <p className="m-0 leading-relaxed">
            <strong>Drive it like a web page.</strong> Every native control is a real HTML element (
            <Code>{"<button>"}</Code>, <Code>{"<input>"}</Code>, <Code>{"<a>"}</Code>) laid over the
            live screen — read the ghost DOM, then click and type as if it were real. macOS is
            basically HTML here; your clicks and keystrokes become real input to the app.
          </p>
        </div>
        <ol className="my-3 list-decimal space-y-2 pl-6">
          <li>
            <strong>Read the ghost DOM to find a control.</strong> Each one is an accessible element
            with a label and an accurate box, positioned over the stream.
          </li>
          <li>
            <strong>Can&rsquo;t find what you need?</strong> The app&rsquo;s accessibility is thin —
            screenshot the stream and read it visually instead.
          </li>
          <li>
            <strong>Screenshot doesn&rsquo;t match the DOM?</strong> Both the image and the ghost DOM
            trail the app&rsquo;s real state (the image lags most). Wait a moment, then screenshot
            again.
          </li>
          <li>
            <strong>Act like it&rsquo;s a web page.</strong> Click a control (the element, or any
            spot on the stream), then type. <strong>Keys only reach the app while the green</strong>{" "}
            <Code>⌨ keyboard captured</Code> <strong>badge (bottom-right) is lit</strong> — clicking
            into the app turns it on; it&rsquo;s off whenever the app doesn&rsquo;t have focus.
          </li>
        </ol>
        <p className="my-2 text-[13px] leading-relaxed text-neutral-500 dark:text-neutral-400">
          Every click and keystroke becomes real macOS input — the actual cursor moves and the app
          takes focus, so there&rsquo;s no DOM event on the ghost node. Re-read the ghost DOM after
          acting to see what changed.
        </p>

        <H2>Typing into an app</H2>
        <p className="my-3">
          Ghost text fields are <strong>non-editable mirrors</strong> of the app&rsquo;s real fields
          — setting their value does nothing. Click the field in the app, then use the{" "}
          <strong>compose bar</strong> under the stage: it is a real HTML input, so type your text
          there and press <Code>Send</Code> (or <Code>Send ↵</Code> to submit it in one go). It shows
          which control the app&rsquo;s keyboard focus is currently on, so you can tell the text will
          land where you expect.
        </p>
        <p className="my-3">
          Next to it is a row of <strong>key buttons</strong> — <Code>Enter</Code>, <Code>Tab</Code>,{" "}
          <Code>Esc</Code>, arrows, <Code>⌘A</Code>, <Code>⌘C</Code>, <Code>⌘V</Code>. If your harness
          can only fill text inputs and click, those buttons are how you press keys that typing
          alone can&rsquo;t express.
        </p>

        <H2>Terminal — the non-GUI route</H2>
        <div className="my-3 rounded-lg border border-emerald-200 bg-emerald-50 px-4 py-3 dark:border-emerald-900 dark:bg-emerald-950/40">
          <p className="m-0 leading-relaxed">
            <strong>Terminal is a special app in the sidebar</strong>, and the same session is
            stacked under every app page so you can watch the GUI and the shell at once. It is a
            real <Code>zsh</Code> session on this <strong>macOS</strong> host — not a terminal app
            being driven through the GUI.
          </p>
        </div>
        <p className="my-3">
          <strong>Prefer it whenever a command is shorter or more reliable than clicking.</strong>{" "}
          To change the volume, don&rsquo;t hunt for a slider — run{" "}
          <Code>osascript -e &apos;set volume output volume 30&apos;</Code>. Same for reading and
          writing files, opening URLs (<Code>open -a Safari https://…</Code>), the clipboard (
          <Code>pbcopy</Code> / <Code>pbpaste</Code>), system settings (<Code>defaults write</Code>),
          git, and package managers.
        </p>
        <p className="my-3">
          <strong>To run a command: type it in the box and click Run.</strong> That&rsquo;s all —
          no API call and no page refresh. The output appears above the box and updates by itself,
          so just re-read the page. A green <Code>running</Code> line shows while a command is still
          going; the <Code>^C</Code> button interrupts it, <Code>^D</Code> sends EOF, and there are
          Enter, Tab, Escape and arrow buttons (arrows recall previous commands).
        </p>
        <p className="my-3">
          It is a pty, so <strong>cwd, environment, virtualenvs and history persist</strong> between
          commands, and interactive programs work (<Code>python</Code>, <Code>ssh</Code>,{" "}
          <Code>vim</Code>, anything that prompts) — answer their prompts by typing in the same box.
          There is no exit code shown: read the output, or end the command with{" "}
          <Code>; echo EXIT=$?</Code>.
        </p>
        <p className="my-2 text-[13px] leading-relaxed text-neutral-500 dark:text-neutral-400">
          If your harness can make HTTP requests, the same session is also at{" "}
          <Code>POST /api/term/main/input</Code> with{" "}
          <Code>{'{"value":"ls -la","enter":true}'}</Code>, whose reply carries the rendered screen
          plus <Code>busy</Code> (a command is still running) and <Code>settled</Code> (output went
          quiet). The UI needs none of it.
        </p>

        <p className="mt-10 border-t border-neutral-200 pt-4 text-[13px] text-neutral-500 dark:border-neutral-800 dark:text-neutral-400">
          Pick an app from the sidebar to open its page. Toggle <strong>Outline elements</strong>{" "}
          (or add <Code>?debug=1</Code> to the URL) to visualize the ghost DOM over the stream.
        </p>
      </div>
    </div>
  );
}
