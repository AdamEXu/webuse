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

        <p className="mt-10 border-t border-neutral-200 pt-4 text-[13px] text-neutral-500 dark:border-neutral-800 dark:text-neutral-400">
          Pick an app from the sidebar to open its page. Toggle <strong>Outline elements</strong>{" "}
          (or add <Code>?debug=1</Code> to the URL) to visualize the ghost DOM over the stream.
        </p>
      </div>
    </div>
  );
}
