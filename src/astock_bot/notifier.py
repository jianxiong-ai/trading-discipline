from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from urllib.request import Request, urlopen

from .models import Signal


def feishu_signature(timestamp: int, secret: str) -> str:
    string_to_sign = f"{timestamp}\n{secret}".encode("utf-8")
    digest = hmac.new(string_to_sign, digestmod=hashlib.sha256).digest()
    return base64.b64encode(digest).decode("utf-8")


def render_message(
    signals: list[Signal],
    node: str,
    title: str,
    evidence_char_limit: int = 240,
    margin_char_limit: int = 120,
) -> str:
    lines = [f"【{title}｜{node}】"]
    for index, signal in enumerate(signals):
        if index:
            lines.extend(["", "━━━━━━━━━━", ""])
        change_pct = signal.details.get("change_pct")
        change_text = f"{float(change_pct):+.2f}%" if change_pct is not None else "涨跌幅暂缺"
        # Keep the instrument/price line compact, then use explicit section
        # headings and blank lines so Feishu's plain-text card remains scannable.
        lines.append(f"{signal.name}（{signal.symbol}） {signal.price:.2f}（{change_text}）")
        lines.append("【操作建议】")
        satellite_exit = int(signal.details.get("satellite_exit_shares", 0) or 0)
        if satellite_exit:
            action_parts = [f"退出卫星仓{satellite_exit}股"]
            if signal.shares:
                action_parts.append(f"降低主仓{signal.shares}股")
            lines.append("建议：" + "；".join(action_parts))
        else:
            planned_nav_ratio = signal.details.get("planned_nav_ratio")
            weight_text = (
                f"（计划金额约占账户资产{float(planned_nav_ratio):.1%}）"
                if planned_nav_ratio is not None and signal.shares > 0
                else ""
            )
            share_text = f" {signal.shares}股{weight_text}" if signal.shares > 0 else ""
            lines.append(f"建议：{signal.action}{share_text}")
        if signal.details.get("pending_reminder"):
            lines.append("状态：前次建议尚未确认成交，本次为待处理提醒")

        lines.extend(["", "【触发原因】", f"原因：{signal.reason}"])
        evidence = str(signal.details.get("evidence") or "").strip()
        if evidence and signal.code in {
            "UP_BREAK", "DOWN_BREAK", "SAT_BUY", "STAGE_REENTRY", "STAGE_TOP_EXIT",
            "MIGRATION_TRIM", "FALSE_BREAK", "WATCH_ENTRY", "WATCH_NEAR_ENTRY",
        }:
            evidence_body, margin_evidence, capital_evidence, holder_evidence = (
                _partition_auxiliary_evidence(evidence)
            )
            evidence_lines: list[str] = []
            if evidence_body:
                evidence_lines.append(f"证据：{_clip(evidence_body, evidence_char_limit)}")
            if margin_evidence:
                evidence_lines.append(
                    "两融："
                    + _clip(
                        margin_evidence.removeprefix("两融日终："),
                        margin_char_limit,
                    )
                )
            if capital_evidence:
                evidence_lines.append(
                    "资金："
                    + _clip(
                        capital_evidence.removeprefix("资金日终："),
                        margin_char_limit,
                    )
                )
            if holder_evidence:
                evidence_lines.append(
                    "筹码："
                    + _clip(
                        holder_evidence.removeprefix("股东户数："),
                        margin_char_limit,
                    )
                )
            if evidence_lines:
                lines.extend(["", "【证据】", *evidence_lines])
        if signal.code in {"OVERHEAT_WATCH", "WATCH_NEAR_ENTRY"}:
            lines.extend(["", "级别：提醒（非买卖指令）"])
        references = []
        if "target" in signal.details:
            if signal.code == "SAT_BUY":
                label = "计划止盈价"
            elif signal.code in {
                "UP_BREAK", "STAGE_REENTRY", "WATCH_ENTRY", "WATCH_NEAR_ENTRY",
            }:
                label = "计划目标价"
            else:
                label = "原计划止盈价"
            references.append(f"{label} {signal.details['target']:.2f}")
        if "stop" in signal.details:
            references.append(f"风险退出价 {signal.details['stop']:.2f}")
        references.append(f"条件失效：{signal.invalidation}")
        lines.extend(["", "【执行参考】", "参考：" + "；".join(references)])
        if "holding_days" in signal.details:
            lines.append(f"持有：{signal.details['holding_days']}个交易日")
    if any(signal.code == "WATCH_ENTRY" for signal in signals):
        footer = "仅作纪律提醒，不自动下单；首次建仓成交后请填写股数和经济投入，并将role改为holding。"
    else:
        footer = "仅作纪律提醒，不自动下单；成交后请更新配置。"
    lines.extend(["", "──────────", footer])
    return "\n".join(lines)


def render_daily_summary(
    rows: list[dict],
    nodes: list[str],
    day: str,
    title: str,
    warnings: list[str] | None = None,
    ledger_notes: list[str] | None = None,
) -> str:
    triggered = sum(int(row.get("trigger_count", 0)) for row in rows)
    headline = "今日无操作信号，继续按纪律观察。" if not triggered else f"今日共记录{triggered}个操作信号，请复核未确认成交的建议。"
    lines = [f"【{title}｜日内总结 {day}】", f"结论：{headline}"]
    for row in rows:
        lines.append("")
        price = row.get("price")
        price_text = f"{float(price):.2f}" if price is not None else "价格暂缺"
        change_pct = row.get("change_pct")
        change_text = f"{float(change_pct):+.2f}%" if change_pct is not None else "涨跌幅暂缺"
        lines.append(f"{row['name']}（{row['symbol']}） {price_text}（{change_text}）")
        lines.append(f"建议：{row['recommendation']}")
        lines.append(f"原因：{row['reason']}")
        status_by_node = row.get("status_by_node", {})
        lines.append("节点：" + "｜".join(f"{node} {status_by_node.get(node, '缺失')}" for node in nodes))
    if warnings:
        lines.extend(["", "数据提示：" + _clip("；".join(dict.fromkeys(warnings)), 180)])
    if ledger_notes:
        lines.extend(["", "台账更新：" + _clip("；".join(dict.fromkeys(ledger_notes)), 180)])
    lines.append("仅作当日纪律复盘，不代表已成交，也不会自动下单。")
    return "\n".join(lines)


def _clip(value: str, limit: int) -> str:
    return value if len(value) <= limit else value[: limit - 1] + "…"


def _partition_margin_evidence(value: str) -> tuple[str, str]:
    body, margin, _, _ = _partition_auxiliary_evidence(value)
    return body, margin


def _partition_auxiliary_evidence(value: str) -> tuple[str, str, str, str]:
    parts = [part.strip() for part in value.split("；") if part.strip()]
    margin_parts = [part for part in parts if part.startswith("两融日终：")]
    capital_parts = [part for part in parts if part.startswith("资金日终：")]
    holder_parts = [part for part in parts if part.startswith("股东户数：")]
    body_parts = [
        part
        for part in parts
        if not part.startswith("两融日终：")
        and not part.startswith("资金日终：")
        and not part.startswith("股东户数：")
    ]
    return (
        "；".join(body_parts),
        "；".join(margin_parts),
        "；".join(capital_parts),
        "；".join(holder_parts),
    )


class FeishuNotifier:
    def __init__(self, webhook: str, secret: str = "", timeout: int = 8):
        self.webhook = webhook.strip()
        self.secret = secret.strip()
        self.timeout = timeout

    def send(self, text: str) -> None:
        if not self.webhook:
            raise RuntimeError("未配置 FEISHU_WEBHOOK")
        payload = {"msg_type": "text", "content": {"text": text}}
        if self.secret:
            timestamp = int(time.time())
            payload.update({"timestamp": str(timestamp), "sign": feishu_signature(timestamp, self.secret)})
        request = Request(
            self.webhook,
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={"Content-Type": "application/json; charset=utf-8"},
            method="POST",
        )
        with urlopen(request, timeout=self.timeout) as response:
            body = json.loads(response.read().decode("utf-8"))
        code = body.get("code", body.get("StatusCode", 0))
        if code not in (0, None):
            raise RuntimeError(f"飞书发送失败: {body}")
