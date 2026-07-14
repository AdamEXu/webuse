"""webuse — serve macOS apps as live, agent-drivable web pages."""

import argparse
import signal
import threading

import uvicorn


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8787)
    p.add_argument("--no-cursor", action="store_true", help="disable the on-screen agent cursor")
    args = p.parse_args()

    from webuse import ax, cursor
    from webuse.server import TOKEN, app

    print(f"webuse on http://{args.host}:{args.port}")
    print(f"  auth token (for non-localhost clients): {TOKEN}")
    print(f"  tunnel:  cloudflared tunnel --url http://127.0.0.1:{args.port}")
    print(f"           then share https://<tunnel>/?token={TOKEN}")
    if not ax.trusted():
        print("  ⚠ Accessibility permission NOT granted — the ghost DOM will be empty.")
    if not ax.screen_recording():
        print("  ⚠ Screen Recording permission NOT granted — app streams will be black.")
    if not (ax.trusted() and ax.screen_recording()):
        print(f"    Open http://{args.host}:{args.port} for guided setup.")

    config = uvicorn.Config(app, host=args.host, port=args.port, log_level="warning")
    server = uvicorn.Server(config)

    # The on-screen agent cursor is an AppKit overlay that must own the main
    # thread, so run uvicorn on a background thread and the Cocoa loop on main.
    # Falls back to a plain server when there's no GUI session (headless).
    if not args.no_cursor and cursor.build():
        print("  on-screen agent cursor: on")
        signal.signal(signal.SIGINT, signal.SIG_DFL)  # Ctrl+C exits under NSApp.run()
        threading.Thread(target=server.run, daemon=True).start()
        cursor.run()  # blocks on the AppKit event loop
    else:
        server.run()


if __name__ == "__main__":
    main()
