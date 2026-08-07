import Terminal from "./Terminal";

// The hidden agent-note (agents read it), mirroring the Driver's. Terminal state
// lives on the server, so this note describes the same shell the stacked panel
// on every app page is showing.
const AGENT_NOTE =
  `This is a REAL zsh shell running on the macOS host, in a persistent session. ` +
  `TO RUN A COMMAND: type it into the text box labelled "zsh command", then ` +
  `CLICK THE "Run" BUTTON. That is the whole interaction — no API call, no ` +
  `JavaScript, no page refresh needed. The output appears in the black area ` +
  `above the box and updates BY ITSELF as the command writes, so just re-read ` +
  `this page to see the result. ` +
  `The buttons below the box press keys you cannot otherwise send: "^C" ` +
  `interrupts a running command, "^D" sends EOF (exits a REPL), "clear" clears ` +
  `the screen, and there are Enter, Tab, Escape and arrow keys (arrows recall ` +
  `previous commands). A green "running" line appears while a command is still ` +
  `going; when it disappears the command has finished. ` +
  `PREFER THIS OVER CLICKING THE GUI for anything a command can do — it is ` +
  `faster and far more reliable than hunting for controls. Examples: ` +
  `osascript -e 'set volume output volume 30' (volume), ` +
  `open -a Safari https://example.com (open a URL), pbpaste / pbcopy ` +
  `(clipboard), defaults read/write (system prefs), plus files, processes, git ` +
  `and package managers. ` +
  `Because it is a pty, cwd, environment, activated virtualenvs and history ` +
  `PERSIST between commands, and interactive programs work (python, ssh, vim, ` +
  `anything that prompts) — type into the same box to answer a prompt. ` +
  `There is no exit code shown: read the output, or end the command with ` +
  `'; echo EXIT=$?'. ` +
  `If your harness CAN make HTTP requests, the same session is also at ` +
  `POST /api/term/main/input {"value":"ls -la","enter":true}, whose reply ` +
  `carries the rendered screen plus "busy" (a command is still running) and ` +
  `"settled" (output went quiet) — but the UI above needs none of that.`;

// Standalone terminal route. The same session is embedded under every app page,
// so switching here is a bigger view of one shell, not a different one.
export default function TerminalView() {
  return (
    <>
      <div id="agent-note">{AGENT_NOTE}</div>
      <main id="main" className="term-page">
        <header className="term-head">
          <h1>Terminal</h1>
          <p>
            A real zsh session on this Mac — persistent cwd, env and history. Use it
            instead of clicking through the GUI for anything a command can do. It is
            also embedded under every app page.
          </p>
        </header>
        <Terminal sid="main" autoFocus />
      </main>
    </>
  );
}
