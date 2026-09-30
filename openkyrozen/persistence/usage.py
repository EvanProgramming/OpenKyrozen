from __future__ import annotations

import sqlite3
import uuid
from typing import Any

from .models import utc_now, stable_hash

class UsageRepository:
    def __init__(self, database):
        self.database = database

    def record_usage_attempt(
            self, *, attempt_id: str, provider: str, model: str, surface: str,
            user_id: str = "local", workspace_id: str = "default",
            session_id: str | None = None, run_id: str | None = None,
            cache_status: str = "unknown", prompt_tokens: int | None = None,
            cache_hit_tokens: int | None = None, cache_miss_tokens: int | None = None,
            completion_tokens: int | None = None, reasoning_tokens: int | None = None,
            latency_ms: int | None = None, completion_state: str = "completed",
            usage_status: str = "unknown", cost_picos: int | None = None,
            pricing_snapshot: dict[str, Any] | None = None,
    ) -> bool:
        """Store one immutable provider attempt; duplicate IDs are harmless."""
        def integer(value: int | None) -> int | None:
            return None if value is None else max(0, int(value))

        values = (
            str(attempt_id)[:128], str(provider or "unknown")[:64], str(model or "unknown")[:128],
            str(surface or "unknown")[:64], str(user_id or "local")[:128],
            str(workspace_id or "default")[:256], str(session_id)[:128] if session_id else None,
            str(run_id)[:128] if run_id else None, str(cache_status or "unknown")[:32],
            integer(prompt_tokens), integer(cache_hit_tokens), integer(cache_miss_tokens),
            integer(completion_tokens), integer(reasoning_tokens), integer(latency_ms),
            str(completion_state or "unknown")[:32], str(usage_status or "unknown")[:32],
            integer(cost_picos), self.database._json(pricing_snapshot or {}), utc_now(),
        )
        with self.database._lock, self.database.connection() as db:
            return db.execute(
                "INSERT OR IGNORE INTO usage_attempts("
                "id,provider,model,surface,user_id,workspace_id,session_id,run_id,cache_status,"
                "prompt_tokens,cache_hit_tokens,cache_miss_tokens,completion_tokens,reasoning_tokens,"
                "latency_ms,completion_state,usage_status,cost_picos,pricing_snapshot,created_at"
                ") VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                values,
            ).rowcount == 1


    def list_usage_attempts(
            self, *, workspace_id: str | None = None, session_id: str | None = None,
            user_id: str | None = None, run_id: str | None = None, limit: int = 1000,
    ) -> list[dict[str, Any]]:
        clauses = ["1=1"]
        params: list[Any] = []
        if workspace_id is not None:
            clauses.append("workspace_id=?")
            params.append(workspace_id)
        if session_id is not None:
            clauses.append("session_id=?")
            params.append(session_id)
        if user_id is not None:
            clauses.append("user_id=?")
            params.append(user_id)
        if run_id is not None:
            clauses.append("run_id=?")
            params.append(run_id)
        params.append(max(1, min(limit, 10000)))
        with self.database.connection() as db:
            rows = db.execute(
                f"SELECT * FROM usage_attempts WHERE {' AND '.join(clauses)} "
                "ORDER BY created_at DESC LIMIT ?", params,
            ).fetchall()
        return [dict(row, pricing_snapshot=self.database._loads(row["pricing_snapshot"], {})) for row in rows]


    def usage_totals(
            self, *, workspace_id: str | None = None, session_id: str | None = None,
            user_id: str | None = None, run_id: str | None = None, since: str | None = None,
    ) -> dict[str, Any]:
        """Aggregate immutable usage records for one explicit durable scope."""
        clauses = ["1=1"]
        params: list[Any] = []
        for field, value in (("workspace_id", workspace_id), ("session_id", session_id),
                             ("user_id", user_id), ("run_id", run_id)):
            if value is not None:
                clauses.append(f"{field}=?")
                params.append(value)
        if since:
            clauses.append("created_at>?")
            params.append(since)
        columns = (
            "COUNT(*) AS attempts, "
            "SUM(CASE WHEN usage_status='authoritative' THEN 1 ELSE 0 END) AS authoritative_attempts, "
            "SUM(CASE WHEN usage_status='estimated' THEN 1 ELSE 0 END) AS estimated_attempts, "
            "SUM(COALESCE(prompt_tokens, 0)) AS prompt_tokens, "
            "SUM(COALESCE(cache_hit_tokens, 0)) AS cache_hit_tokens, "
            "SUM(COALESCE(cache_miss_tokens, 0)) AS cache_miss_tokens, "
            "SUM(COALESCE(completion_tokens, 0)) AS completion_tokens, "
            "SUM(COALESCE(reasoning_tokens, 0)) AS reasoning_tokens, "
            "SUM(COALESCE(cost_picos, 0)) AS cost_picos"
        )
        with self.database.connection() as db:
            total = db.execute(
                f"SELECT {columns} FROM usage_attempts WHERE {' AND '.join(clauses)}", params,
            ).fetchone()
            by_provider = db.execute(
                f"SELECT provider, {columns} FROM usage_attempts WHERE {' AND '.join(clauses)} "
                "GROUP BY provider ORDER BY provider", params,
            ).fetchall()
            pricing_groups = db.execute(
                "SELECT provider, pricing_snapshot, "
                "SUM(COALESCE(prompt_tokens, 0)) AS prompt_tokens, "
                "SUM(COALESCE(cache_hit_tokens, 0)) AS cache_hit_tokens, "
                "SUM(COALESCE(cache_miss_tokens, 0)) AS cache_miss_tokens, "
                "SUM(COALESCE(completion_tokens, 0)) AS completion_tokens, "
                "SUM(COALESCE(cost_picos, 0)) AS cost_picos "
                f"FROM usage_attempts WHERE {' AND '.join(clauses)} "
                "GROUP BY provider, pricing_snapshot",
                params,
            ).fetchall()

        def normalise(row: sqlite3.Row | None) -> dict[str, int]:
            names = ("attempts", "authoritative_attempts", "estimated_attempts", "prompt_tokens",
                     "cache_hit_tokens", "cache_miss_tokens", "completion_tokens", "reasoning_tokens",
                     "cost_picos")
            values = {name: int((row[name] if row else 0) or 0) for name in names}
            values["unknown_attempts"] = max(
                0, values["attempts"] - values["authoritative_attempts"] - values["estimated_attempts"],
            )
            return values

        def exact_cost(groups: list[sqlite3.Row]) -> int:
            """Round once after combining frozen per-million pricing numerators."""
            numerator = fallback = 0
            for row in groups:
                snapshot = self.database._loads(row["pricing_snapshot"], {})
                try:
                    output_rate = max(0, int(snapshot["output_picos_per_million"]))
                    cache_miss_rate = snapshot.get("cache_miss_picos_per_million")
                    input_rate = max(0, int(
                        cache_miss_rate if cache_miss_rate is not None else snapshot["input_picos_per_million"],
                    ))
                except (KeyError, TypeError, ValueError):
                    # Compatibility for any manually-created legacy ledger row.
                    fallback += int(row["cost_picos"] or 0)
                    continue
                prompt = int(row["prompt_tokens"] or 0)
                if "cache_hit_picos_per_million" in snapshot:
                    hit = min(prompt, max(0, int(row["cache_hit_tokens"] or 0)))
                    numerator += (hit * max(0, int(snapshot["cache_hit_picos_per_million"]))
                                  + (prompt - hit) * input_rate)
                else:
                    numerator += prompt * input_rate
                numerator += int(row["completion_tokens"] or 0) * output_rate
            return fallback + numerator // 1_000_000

        totals = normalise(total)
        totals["cost_picos"] = exact_cost(pricing_groups)
        provider_groups: dict[str, list[sqlite3.Row]] = {}
        for row in pricing_groups:
            provider_groups.setdefault(row["provider"], []).append(row)

        return {
            **totals, "currency": "USD",
            "providers": [dict(provider=row["provider"], **{
                **normalise(row), "cost_picos": exact_cost(provider_groups[row["provider"]]),
            }) for row in by_provider],
        }


    def create_usage_reset(
            self, *, scope: str, user_id: str = "local", workspace_id: str = "default",
            session_id: str | None = None,
    ) -> dict[str, str]:
        """Start a new reported usage window while retaining immutable attempts."""
        if scope not in {"workspace", "session"}:
            raise ValueError("usage reset scope must be workspace or session")
        if scope == "session" and not session_id:
            raise ValueError("session usage reset requires a session_id")
        reset_id = f"usage_reset_{uuid.uuid4().hex}"
        event_id = f"evt_{uuid.uuid4().hex}"
        created_at = utc_now()
        payload = {"reset_id": reset_id, "scope": scope}
        with self.database._lock, self.database.connection() as db:
            db.execute(
                "INSERT INTO usage_resets(id,scope,user_id,workspace_id,session_id,created_at) VALUES(?,?,?,?,?,?)",
                (reset_id, scope, user_id, workspace_id, session_id, created_at),
            )
            db.execute(
                "INSERT INTO events(id,event_type,payload,user_id,workspace_id,session_id,task_id,created_at) "
                "VALUES(?,?,?,?,?,?,?,?)",
                (event_id, "usage.reset", self.database._json(payload), user_id, workspace_id,
                 session_id, None, created_at),
            )
        return {"reset_id": reset_id, "created_at": created_at}


    def latest_usage_reset(
            self, *, scope: str, user_id: str = "local", workspace_id: str = "default",
            session_id: str | None = None,
    ) -> dict[str, Any] | None:
        clauses = ["scope=?", "user_id=?", "workspace_id=?"]
        params: list[Any] = [scope, user_id, workspace_id]
        if scope == "session":
            clauses.append("session_id=?")
            params.append(session_id)
        with self.database.connection() as db:
            row = db.execute(
                f"SELECT * FROM usage_resets WHERE {' AND '.join(clauses)} "
                "ORDER BY created_at DESC LIMIT 1", params,
            ).fetchone()
        return dict(row) if row else None
