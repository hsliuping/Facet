"""Command line entry point for the local channel router."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import subscriptions as subs
from .core import PROTOCOLS, load_config
from .server import make_server


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="planrouter",
        description="Local multi-provider LLM channel router with automatic quota-cooldown failover.",
    )
    parser.add_argument("--config", default="router.json", help="path to the channel config JSON")
    parser.add_argument("--subscriptions", help="subscription records (default: next to config)")
    parser.add_argument("--state-file", help="persisted cooldown state (default: next to config)")
    parser.add_argument("--usage-file", help="daily usage buckets (default: next to config)")
    parser.add_argument("--notifications-file", help="reminder delivery log (default: next to config)")
    parser.add_argument("--host", help="override listen host")
    parser.add_argument("--port", type=int, help="override listen port")
    parser.add_argument("--check", action="store_true", help="validate config and print routes, then exit")
    args = parser.parse_args(argv)

    try:
        config = load_config(Path(args.config))
    except (ValueError, OSError, json.JSONDecodeError) as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return 2

    config_dir = Path(args.config).resolve().parent
    subs_path = Path(args.subscriptions) if args.subscriptions else config_dir / "subscriptions.json"
    state_path = Path(args.state_file) if args.state_file else config_dir / "router.state.json"
    usage_path = Path(args.usage_file) if args.usage_file else config_dir / "usage.json"
    notify_path = (Path(args.notifications_file) if args.notifications_file
                   else config_dir / "notifications.json")

    if args.check:
        print(f"config OK: {args.config}")
        for channel in config.channels:
            print(
                f"  [{channel.protocol:9}] {channel.name:16} priority={channel.priority:<4} "
                f"models={','.join(sorted(channel.models)[:4])} "
                f"cooldown={channel.cooldown_minutes}min "
                f"key={'set' if channel.api_key else 'MISSING'}"
            )
        for key in sorted(config.routes):
            print(f"  route {key:22} -> {' -> '.join(config.routes[key].channels)}")
        print(f"  default cooldown: {config.defaults.cooldown_minutes} min; "
              f"max failover: {config.defaults.max_failover}; "
              f"sticky sessions: {config.defaults.sticky_minutes} min")
        print(f"  state file: {state_path}")
        try:
            records = subs.load_file(subs_path)
            summary = subs.summarize(records)
            print(f"  subscriptions: {summary['total']} ({subs_path})")
            if summary["monthly_cost"]:
                cost = ", ".join(f"{amount:g} {code}/month"
                                 for code, amount in summary["monthly_cost"].items())
                print(f"  monthly cost: {cost}")
            for alert in summary["alerts"]:
                label = "已过期" if alert["status"] == "expired" else "即将到期"
                print(f"  RENEW [{label}] {alert['vendor']} / {alert['account'] or '-'} / "
                      f"{alert['plan']} -> {alert['expires_at']} (剩余 {alert['days_left']} 天)")
        except (OSError, ValueError) as exc:
            print(f"  subscriptions error: {exc}", file=sys.stderr)
        return 0

    httpd = make_server(
        config, args.host, args.port, args.config, state_path, subs_path,
        usage_path, notify_path,
    )
    app = httpd.app  # type: ignore[attr-defined]
    host, port = httpd.server_address[:2]
    ui_host = "127.0.0.1" if host in ("0.0.0.0", "::") else host
    print(f"planrouter listening on http://{host}:{port}/v1", flush=True)
    print(f"  management console: http://{ui_host}:{port}/", flush=True)
    print(f"  subscriptions: {subs_path}", flush=True)
    print(f"  persisted state: {state_path}", flush=True)
    print(f"  usage history: {usage_path}", flush=True)
    print(f"  notification log: {notify_path}", flush=True)
    summary = subs.summarize(app.subscriptions)
    if summary["monthly_cost"]:
        cost = ", ".join(f"{amount:g} {code}/month"
                         for code, amount in summary["monthly_cost"].items())
        print(f"  monthly cost: {cost}", flush=True)
    for alert in summary["alerts"]:
        label = "已过期" if alert["status"] == "expired" else "即将到期"
        print(f"  RENEW [{label}] {alert['vendor']} / {alert['account'] or '-'} / "
              f"{alert['plan']} -> {alert['expires_at']} (剩余 {alert['days_left']} 天)", flush=True)
    for protocol in PROTOCOLS:
        route = config.routes.get(f"{protocol}:default")
        if route:
            print(f"  {protocol:9} default: {' -> '.join(route.channels)}", flush=True)
    print("  health: GET /health   stats: GET /stats   models: GET /v1/models", flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        app.stop_background()
        httpd.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
