"""webuse — serve macOS apps as live, agent-drivable web pages."""

import argparse
import os

import uvicorn


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8787)
    p.add_argument(
        "--local", action="store_true",
        help="don't open the tinyfi.sh tunnel (serve on localhost only)",
    )
    args = p.parse_args()

    from webuse import ax, config
    from webuse.server import TOKEN, app

    print(f"webuse on http://{args.host}:{args.port}")
    print(f"  stable URL:  http://{args.host}:{args.port}/?token={TOKEN}")
    print(f"  auth token (for non-localhost clients): {TOKEN}")

    # Public tunnel by default so a fresh `uv run python main.py` just works.
    # Opt out with --local or WEBUSE_NO_TUNNEL=1. Never fatal: on any failure we
    # log a warning and keep serving locally.
    no_tunnel = args.local or os.environ.get("WEBUSE_NO_TUNNEL") == "1"
    if not no_tunnel:
        try:
            from webuse import server, tunnel

            sub = config.subdomain()
            tunnel.start(args.port, sub)
            # Exposed now: require the token for ALL traffic (a raw ssh -R forward
            # arrives as localhost, so the localhost-trust shortcut must be off).
            server.mark_tunnel_active()
            print(f"  public:  https://{sub}.tinyfi.sh/?token={TOKEN}  (if the tunnel connects)")
            print("  ⚠ SECURITY: this URL grants full control of your Mac — share it carefully.")
        except Exception as e:
            print(f"  ⚠ tunnel unavailable ({e}); serving locally only.")
    else:
        print("  tunnel disabled (--local); serving locally only.")

    if not ax.trusted():
        print("  ⚠ Accessibility permission NOT granted — the ghost DOM will be empty.")
    if not ax.screen_recording():
        print("  ⚠ Screen Recording permission NOT granted — app streams will be black.")
    if not (ax.trusted() and ax.screen_recording()):
        print(f"    Open http://{args.host}:{args.port} for guided setup.")

    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
