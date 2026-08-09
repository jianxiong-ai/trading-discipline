from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from .config import load_config
from .service import MonitorService


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="A股主仓与超短线卫星仓纪律监控")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("run", help="持续运行定时任务")
    once = sub.add_parser("once", help="立即执行一个节点")
    once.add_argument("--node", choices=["09:15", "10:15", "13:15", "14:15"], required=True)
    once.add_argument("--dry-run", action="store_true")
    summary = sub.add_parser("summary", help="生成并发送当日日内总结")
    summary.add_argument("--dry-run", action="store_true")
    sub.add_parser("health", help="检查配置是否可读")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    config_path = os.getenv("ASTOCK_CONFIG", "config.yaml")
    config = load_config(config_path)
    if args.command == "health":
        print(json.dumps({"ok": True, "config": str(Path(config_path)), "positions": len(config.positions)}, ensure_ascii=False))
        return 0
    if args.command == "once":
        service = MonitorService(config)
        execution_type = "dry_run" if args.dry_run else "manual"
        print(json.dumps(
            service.run_node(args.node, dry_run=args.dry_run, execution_type=execution_type),
            ensure_ascii=False,
            indent=2,
            default=str,
        ))
        return 0
    if args.command == "summary":
        service = MonitorService(config)
        print(json.dumps(
            service.run_daily_summary(dry_run=args.dry_run),
            ensure_ascii=False,
            indent=2,
            default=str,
        ))
        return 0
    while True:
        try:
            # 每轮重新读取只读挂载的配置，成交确认后无需重启容器。
            config = load_config(config_path)
        except Exception as exc:
            print(json.dumps({"timestamp": datetime.now().isoformat(), "config_error": str(exc)}, ensure_ascii=False), file=sys.stderr, flush=True)
            time.sleep(15)
            continue
        tz = ZoneInfo(config.timezone)
        now = datetime.now(tz)
        window = int(config.raw.get("run_window_seconds", 180))
        recovery_window = max(window, int(config.raw.get("recovery_window_seconds", 900)))
        for node in config.schedule:
            hour, minute = (int(x) for x in node.split(":"))
            scheduled = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
            delta = (now - scheduled).total_seconds()
            if 0 <= delta <= recovery_window:
                service = MonitorService(config)
                if not service.state.claim_run(now.date(), node):
                    continue
                try:
                    execution_type = "scheduled" if delta <= window else "scheduled_recovery"
                    result = service.run_node(node, now=now, execution_type=execution_type)
                    print(json.dumps({"timestamp": now.isoformat(), **result}, ensure_ascii=False, default=str), flush=True)
                except Exception as exc:
                    # Feishu may have accepted the request before a client
                    # timeout. Keep the at-most-once claim to prevent a
                    # duplicate notification on the next scheduler poll.
                    print(json.dumps({"timestamp": now.isoformat(), "node": node, "error": str(exc)}, ensure_ascii=False), file=sys.stderr, flush=True)
        summary_config = config.section("daily_summary")
        if bool(summary_config.get("enabled", False)):
            summary_time = str(summary_config.get("time", "15:30"))
            hour, minute = (int(x) for x in summary_time.split(":"))
            scheduled = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
            delta = (now - scheduled).total_seconds()
            state_key = f"summary:{summary_time}"
            if 0 <= delta <= recovery_window:
                service = MonitorService(config)
                if service.state.claim_run(now.date(), state_key):
                    try:
                        result = service.run_daily_summary(now=now)
                        print(json.dumps({"timestamp": now.isoformat(), **result}, ensure_ascii=False, default=str), flush=True)
                    except Exception as exc:
                        # A summary timeout is also an uncertain delivery;
                        # do not automatically resend it.
                        print(json.dumps({"timestamp": now.isoformat(), "node": summary_time, "error": str(exc)}, ensure_ascii=False), file=sys.stderr, flush=True)
        time.sleep(15)


if __name__ == "__main__":
    raise SystemExit(main())
