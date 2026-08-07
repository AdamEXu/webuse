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
        help="don't open the Cloudflare tunnel (serve on localhost only)",
    )
    p.add_argument(
        "--hostname",
        help="public hostname routed to this machine's tunnel; remembered for later runs",
    )
    p.add_argument(
        "--tunnel-name",
        help="Cloudflare tunnel to run; remembered for later runs",
    )
    args = p.parse_args()

    from webuse import ax, config
    from webuse.server import TOKEN, app

    print(f"webuse on http://{args.host}:{args.port}")
    print(f"  stable URL:  http://{args.host}:{args.port}/?token={TOKEN}")
    print(f"  auth token (for non-localhost clients): {TOKEN}")

    # Publish through the named Cloudflare tunnel by default, so the public URL is
    # a stable hostname. Opt out with --local or WEBUSE_NO_TUNNEL=1. Never fatal:
    # on any failure we explain it and keep serving locally.
    config.set_tunnel(args.hostname, args.tunnel_name)
    no_tunnel = args.local or os.environ.get("WEBUSE_NO_TUNNEL") == "1"
    host, name = config.hostname(), config.tunnel_name()
    if no_tunnel:
        print("  tunnel disabled (--local); serving locally only.")
    elif not (host and name):
        print("  no tunnel configured; serving locally only. One-time setup:")
        print("      cloudflared tunnel login")
        print("      cloudflared tunnel create <name>")
        print("      cloudflared tunnel route dns <name> <hostname>")
        print("      uv run python main.py --hostname <hostname> --tunnel-name <name>")
    else:
        from webuse import server, tunnel

        why = tunnel.preflight()
        if why:
            print(f"  ⚠ tunnel unavailable: {why}")
            print("    serving locally only.")
        else:
            try:
                tunnel.start(args.port, name)
                # Exposed now: require the token for ALL traffic (the tunnel
                # delivers requests to localhost, so localhost-trust must be off).
                server.mark_tunnel_active()
                print(f"  public:  https://{host}/?token={TOKEN}  (once the tunnel connects)")
                print("  ⚠ SECURITY: this URL grants full control of your Mac,")
                print("    including a real shell at /terminal — share it carefully.")
            except Exception as e:
                print(f"  ⚠ tunnel unavailable ({e}); serving locally only.")

    if not ax.trusted():
        print("  ⚠ Accessibility permission NOT granted — the ghost DOM will be empty.")
    if not ax.screen_recording():
        print("  ⚠ Screen Recording permission NOT granted — app streams will be black.")
    if not (ax.trusted() and ax.screen_recording()):
        print("    Grant both in System Settings → Privacy & Security "
              "(Accessibility, Screen Recording), then restart webuse.")

    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
