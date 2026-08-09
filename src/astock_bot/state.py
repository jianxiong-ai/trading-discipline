from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from typing import Any


class StateStore:
    def __init__(self, path: str):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.data = self._load()

    def _load(self) -> dict[str, Any]:
        if not self.path.exists():
            return self._empty()
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            data.setdefault("sent", {})
            data.setdefault("runs", {})
            data.setdefault("migration", {"positions": {}, "groups": {}})
            data.setdefault("notifications", {})
            data.setdefault("stage", {"positions": {}})
            data.setdefault("active_signals", {})
            return data
        except (ValueError, OSError):
            return self._empty()

    @staticmethod
    def _empty() -> dict[str, Any]:
        return {
            "sent": {},
            "runs": {},
            "migration": {"positions": {}, "groups": {}},
            "notifications": {},
            "stage": {"positions": {}},
            "active_signals": {},
        }

    def active_signal(self, symbol: str) -> dict[str, Any]:
        return dict(self.data.setdefault("active_signals", {}).get(symbol, {}))

    def save_active_signal(self, symbol: str, value: dict[str, Any]) -> None:
        self.data.setdefault("active_signals", {})[symbol] = value
        self._save()

    def clear_active_signal(self, symbol: str) -> None:
        active = self.data.setdefault("active_signals", {})
        if symbol in active:
            del active[symbol]
            self._save()

    def migration_state(self) -> dict[str, Any]:
        return self.data.setdefault("migration", {"positions": {}, "groups": {}})

    def save_migration_state(self, value: dict[str, Any]) -> None:
        self.data["migration"] = value
        self._save()

    def stage_state(self, symbol: str) -> dict[str, Any]:
        return dict(
            self.data.setdefault("stage", {"positions": {}})
            .setdefault("positions", {})
            .get(symbol, {})
        )

    def save_stage_state(self, symbol: str, value: dict[str, Any]) -> None:
        positions = self.data.setdefault("stage", {"positions": {}}).setdefault("positions", {})
        positions[symbol] = value
        self._save()

    def already_sent(self, event_id: str) -> bool:
        return event_id in self.data.setdefault("sent", {})

    def sent_rank(self, event_id: str) -> int | None:
        item = self.data.setdefault("sent", {}).get(event_id)
        return None if item is None else int(item.get("rank", 1))

    @staticmethod
    def _semantic_event_key(event_id: str, item: dict[str, Any] | None = None) -> str:
        """Return the date-independent identity used for persistent signals.

        Older state files did not persist ``semantic_key``; deriving it from the
        legacy ``YYYY-MM-DD|...`` event id keeps the migration backwards
        compatible.
        """
        if item and item.get("semantic_key"):
            return str(item["semantic_key"])
        return event_id.split("|", 1)[1] if "|" in event_id else event_id

    def sent_rank_for_semantic_key(self, semantic_key: str) -> int | None:
        ranks = [
            int(item.get("rank", 1))
            for event_id, item in self.data.setdefault("sent", {}).items()
            if self._semantic_event_key(event_id, item) == semantic_key
        ]
        return max(ranks) if ranks else None

    def clear_sent_semantic_key(self, semantic_key: str) -> None:
        sent = self.data.setdefault("sent", {})
        removed = [
            event_id
            for event_id, item in sent.items()
            if self._semantic_event_key(event_id, item) == semantic_key
        ]
        if removed:
            for event_id in removed:
                del sent[event_id]
            self._save()

    def count(self, day: date, category: str) -> int:
        prefix = day.isoformat()
        return sum(1 for item in self.data.setdefault("sent", {}).values() if item.get("date") == prefix and item.get("category") == category)

    def mark_sent(
        self,
        event_id: str,
        day: date,
        category: str,
        rank: int = 1,
        semantic_key: str | None = None,
    ) -> None:
        self.data.setdefault("sent", {})[event_id] = {
            "date": day.isoformat(),
            "category": category,
            "rank": int(rank),
            **({"semantic_key": semantic_key} if semantic_key else {}),
        }
        self._save()

    def notification_count(self, day: date, category: str) -> int:
        return int(
            self.data.setdefault("notifications", {})
            .get(day.isoformat(), {})
            .get(category, 0)
        )

    def mark_notification(self, day: date, categories: set[str]) -> None:
        daily = self.data.setdefault("notifications", {}).setdefault(day.isoformat(), {})
        for category in categories:
            daily[category] = int(daily.get(category, 0)) + 1
        self._save()

    def ran(self, day: date, node: str) -> bool:
        return bool(self.data.setdefault("runs", {}).get(f"{day.isoformat()}|{node}"))

    def claim_run(self, day: date, node: str) -> bool:
        """Persist an at-most-once claim before an external notification call."""
        key = f"{day.isoformat()}|{node}"
        runs = self.data.setdefault("runs", {})
        if runs.get(key):
            return False
        runs[key] = True
        self._prune(day)
        self._save()
        return True

    def release_run(self, day: date, node: str) -> None:
        """Explicit administrative retry; scheduler keeps uncertain sends claimed."""
        self.data.setdefault("runs", {}).pop(f"{day.isoformat()}|{node}", None)
        self._save()

    def mark_ran(self, day: date, node: str) -> None:
        self.data.setdefault("runs", {})[f"{day.isoformat()}|{node}"] = True
        self._prune(day)
        self._save()

    def _prune(self, day: date) -> None:
        cutoff = day.toordinal() - 45
        self.data["runs"] = {k: v for k, v in self.data.get("runs", {}).items() if date.fromisoformat(k[:10]).toordinal() >= cutoff}
        self.data["sent"] = {k: v for k, v in self.data.get("sent", {}).items() if date.fromisoformat(v["date"]).toordinal() >= cutoff}
        self.data["notifications"] = {
            key: value
            for key, value in self.data.get("notifications", {}).items()
            if date.fromisoformat(key).toordinal() >= cutoff
        }
        self.data["active_signals"] = {
            symbol: value
            for symbol, value in self.data.get("active_signals", {}).items()
            if value.get("date")
            and date.fromisoformat(str(value["date"])).toordinal() >= cutoff
        }

    def _save(self) -> None:
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.data, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(self.path)
