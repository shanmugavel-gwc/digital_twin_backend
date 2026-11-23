"""Agent configuration: one JSON document in twin.agent_config (id = 1). Defaults below are used until an admin saves a change.
Every value is a documented rule parameter (docs/ASSUMPTIONS.md A-28..A-31), not a fitted model."""
from __future__ import annotations

import copy
import json

from sqlalchemy import text

from ..deps import get_engine, query

DEFAULTS: dict = {
    "agent_name": "Manufacturing Digital Twin Agent",
    "analysis_window_days": 7,          # findings look at the last N days of data (anchored on the latest hour in the export)
    "sensor_sigma": 3.0,                # sensor excursion = reading above the machine's own baseline mean + k * std
    "risk_weights": {                   # weights of the rule-based risk index (sum 100)
        "anomalies": 25, "sensor_excursions": 20, "down_time": 15, "downtime_events": 15, "maintenance_due": 15, "station_quality": 10,
    },
    "risk_caps": {                      # value at which a component counts fully (score 1.0) in the analysis window
        "anomalies": 5, "sensor_excursions": 6, "down_hours": 24, "downtime_event_minutes": 600,
    },
    "status_thresholds": {"WARNING": 25, "ATTENTION": 40, "CRITICAL": 60},   # risk index bands; OFFLINE = latest state DOWN
    "plan_attainment_min_pct": 90.0,    # production finding below this plan attainment
    "quality_fail_rate_max_pct": 4.0,   # quality finding above this inspection fail rate
    "max_recommendations": 12,
    "supersede_previous": True,         # a new run marks the previous run's still-pending recommendations SUPERSEDED
    "llm_narrative": True,              # ask the LLM (Domo) for an executive summary written ONLY from the computed findings
    "steps": {"notifications": True, "prediction_models": True},
    "notify": {"email_recipients": "", "auto_send": False},   # auto_send false: messages are PREPARED, a person sends them
}


def _merge(base: dict, over: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in (over or {}).items():
        if k in out and isinstance(out[k], dict) and isinstance(v, dict):
            out[k] = _merge(out[k], v)
        elif k in out:
            out[k] = v
    return out


def load() -> dict:
    rows = query("SELECT config, updated_at, updated_by FROM twin.agent_config WHERE id = 1")
    cfg = _merge(DEFAULTS, rows[0]["config"] if rows else {})
    return cfg


def meta() -> dict:
    rows = query("SELECT updated_at, updated_by FROM twin.agent_config WHERE id = 1")
    return {"updated_at": rows[0]["updated_at"].isoformat(), "updated_by": rows[0]["updated_by"]} if rows else {"updated_at": None, "updated_by": None}


class ConfigError(ValueError):
    pass


def validate(cfg: dict) -> dict:
    c = _merge(DEFAULTS, cfg)
    if not (1 <= int(c["analysis_window_days"]) <= 60):
        raise ConfigError("analysis_window_days must be 1..60")
    if not (1.0 <= float(c["sensor_sigma"]) <= 6.0):
        raise ConfigError("sensor_sigma must be 1..6")
    w = c["risk_weights"]
    if any(float(v) < 0 for v in w.values()) or abs(sum(float(v) for v in w.values()) - 100) > 0.01:
        raise ConfigError("risk_weights must be non-negative and sum to 100")
    if any(float(v) <= 0 for v in c["risk_caps"].values()):
        raise ConfigError("risk_caps must be positive")
    t = c["status_thresholds"]
    if not (0 < float(t["WARNING"]) < float(t["ATTENTION"]) < float(t["CRITICAL"]) <= 100):
        raise ConfigError("status_thresholds must satisfy 0 < WARNING < ATTENTION < CRITICAL <= 100")
    if not (1 <= int(c["max_recommendations"]) <= 50):
        raise ConfigError("max_recommendations must be 1..50")
    return c


def save(cfg: dict, actor: str) -> dict:
    c = validate(cfg)
    from ..twin.scenarios import audit
    with get_engine().begin() as conn:
        conn.execute(text("INSERT INTO twin.agent_config (id, config, updated_by) VALUES (1, CAST(:c AS jsonb), :u) "
                          "ON CONFLICT (id) DO UPDATE SET config = EXCLUDED.config, updated_at = now(), updated_by = EXCLUDED.updated_by"),
                     dict(c=json.dumps(c), u=actor))
        audit(conn, actor, "AGENT_CONFIG_UPDATED", "agent_config", "1", {"config": c})
    return c
