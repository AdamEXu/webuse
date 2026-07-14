"""webuse — serve macOS apps as live, agent-drivable web pages."""

import argparse

import uvicorn


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8787)
    args = p.parse_args()

    from webuse import ax
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
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
