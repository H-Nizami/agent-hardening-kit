"""Atomic same-host tenant admission. No model credentials or request text stored."""

from contextlib import closing, contextmanager
from dataclasses import dataclass
import hashlib
import json
import math
from pathlib import Path
import re
import sqlite3
import time

from .models import Answer, Quote, Request, TenantPolicy, Usage


class AdmissionDenied(Exception):
    def __init__(self, reason):
        super().__init__(reason)
        self.reason = reason


class SettlementConflict(ValueError):
    pass


@dataclass(frozen=True)
class Reservation:
    tenant_id: str
    request_id: str
    call_id: str
    input_hash: str
    status: str
    quoted_tokens: int
    quoted_cost: int
    response: dict | None = None
    created: bool = False


class BudgetStore:
    def __init__(self, path, policies=None, *, clock=time.time):
        if str(path) == ":memory:":
            raise ValueError("BudgetStore requires a file-backed database")
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.clock = clock
        with closing(self._connect()) as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.executescript("""
                CREATE TABLE IF NOT EXISTS tenants (
                    tenant TEXT PRIMARY KEY, token_limit INTEGER NOT NULL,
                    cost_limit INTEGER NOT NULL, rate_limit INTEGER NOT NULL,
                    window_seconds INTEGER NOT NULL, frozen INTEGER NOT NULL DEFAULT 0,
                    last_window INTEGER
                );
                CREATE TABLE IF NOT EXISTS windows (
                    tenant TEXT NOT NULL, window INTEGER NOT NULL, admissions INTEGER NOT NULL,
                    PRIMARY KEY(tenant, window)
                );
                CREATE TABLE IF NOT EXISTS reservations (
                    tenant TEXT NOT NULL, request TEXT NOT NULL, call TEXT NOT NULL,
                    input_hash TEXT NOT NULL, status TEXT NOT NULL,
                    quoted_tokens INTEGER NOT NULL, quoted_cost INTEGER NOT NULL,
                    charged_tokens INTEGER NOT NULL, charged_cost INTEGER NOT NULL,
                    response TEXT, settlement_hash TEXT,
                    PRIMARY KEY(tenant, request, call)
                );
            """)
        if policies:
            with self._transaction() as connection:
                for tenant, value in policies.items():
                    # Reuse the strict transport identifier validation.
                    Request(tenant_id=tenant, request_id="configuration", question="configuration")
                    policy = value if isinstance(value, TenantPolicy) else TenantPolicy.model_validate(value)
                    connection.execute("""
                        INSERT INTO tenants(tenant,token_limit,cost_limit,rate_limit,window_seconds)
                        VALUES(?,?,?,?,?) ON CONFLICT(tenant) DO UPDATE SET
                        token_limit=excluded.token_limit,cost_limit=excluded.cost_limit,
                        rate_limit=excluded.rate_limit,window_seconds=excluded.window_seconds
                    """, (tenant, policy.token_limit, policy.cost_limit_microusd,
                          policy.requests_per_window, policy.window_seconds))

    def _connect(self):
        connection = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout=10000")
        return connection

    @contextmanager
    def _transaction(self):
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            yield connection
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    @staticmethod
    def _reservation(row, *, created=False):
        return Reservation(row["tenant"], row["request"], row["call"], row["input_hash"],
                           row["status"], row["quoted_tokens"], row["quoted_cost"],
                           json.loads(row["response"]) if row["response"] is not None else None,
                           created)

    def reserve(self, tenant_id, request_id, call_id, input_hash, quote):
        Request(tenant_id=tenant_id, request_id=request_id, question="admission")
        Request(tenant_id=tenant_id, request_id=call_id, question="admission")
        if not isinstance(input_hash, str) or not re.fullmatch(r"[a-f0-9]{64}", input_hash):
            raise ValueError("input digest must be SHA256")
        quote = quote if isinstance(quote, Quote) else Quote.model_validate(quote)
        moment = self.clock()
        if type(moment) not in (int, float) or not math.isfinite(moment) or moment < 0:
            raise ValueError("invalid internal clock")
        with self._transaction() as connection:
            row = connection.execute("SELECT * FROM reservations WHERE tenant=? AND request=? AND call=?",
                                     (tenant_id, request_id, call_id)).fetchone()
            if row:
                if row["input_hash"] != input_hash:
                    raise AdmissionDenied("request_conflict")
                return self._reservation(row)
            policy = connection.execute("SELECT * FROM tenants WHERE tenant=?", (tenant_id,)).fetchone()
            if policy is None:
                raise AdmissionDenied("unknown_tenant")
            if policy["frozen"]:
                raise AdmissionDenied("tenant_frozen")
            spent = connection.execute("SELECT COALESCE(SUM(charged_tokens),0),COALESCE(SUM(charged_cost),0) FROM reservations WHERE tenant=?", (tenant_id,)).fetchone()
            if spent[0] + quote.total_tokens > policy["token_limit"] or spent[1] + quote.cost_microusd > policy["cost_limit"]:
                raise AdmissionDenied("budget_exhausted")
            window = int(moment // policy["window_seconds"])
            if policy["last_window"] is not None:
                window = max(window, policy["last_window"])
            rate = connection.execute("SELECT admissions FROM windows WHERE tenant=? AND window=?", (tenant_id, window)).fetchone()
            if rate and rate[0] >= policy["rate_limit"]:
                raise AdmissionDenied("rate_limited")
            connection.execute("INSERT INTO windows VALUES(?,?,1) ON CONFLICT(tenant,window) DO UPDATE SET admissions=admissions+1", (tenant_id, window))
            connection.execute("UPDATE tenants SET last_window=? WHERE tenant=?", (window, tenant_id))
            connection.execute("INSERT INTO reservations(tenant,request,call,input_hash,status,quoted_tokens,quoted_cost,charged_tokens,charged_cost) VALUES(?,?,?,?,'reserved',?,?,?,?)",
                               (tenant_id, request_id, call_id, input_hash, quote.total_tokens,
                                quote.cost_microusd, quote.total_tokens, quote.cost_microusd))
            row = connection.execute("SELECT * FROM reservations WHERE tenant=? AND request=? AND call=?", (tenant_id, request_id, call_id)).fetchone()
            return self._reservation(row, created=True)

    def settle(self, reservation, usage=None, *, response=None, failed=False):
        if type(failed) is not bool:
            raise ValueError("failed must be boolean")
        if usage is not None:
            usage = usage if isinstance(usage, Usage) else Usage.model_validate(usage)
        if response is not None:
            response = response if isinstance(response, Answer) else Answer.model_validate(response)
        payload = {"usage": usage.model_dump() if usage else None,
                   "response": response.model_dump() if response else None, "failed": failed}
        digest = hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()
        with self._transaction() as connection:
            row = connection.execute("SELECT * FROM reservations WHERE tenant=? AND request=? AND call=?",
                                     (reservation.tenant_id, reservation.request_id, reservation.call_id)).fetchone()
            if row is None or row["input_hash"] != reservation.input_hash:
                raise SettlementConflict("reservation identity mismatch")
            if row["status"] != "reserved":
                if row["settlement_hash"] == digest:
                    return self._reservation(row)
                raise SettlementConflict("reservation already settled")
            unknown = failed or usage is None
            tokens = max(row["quoted_tokens"], usage.total_tokens if usage else 0) if unknown else usage.total_tokens
            cost = max(row["quoted_cost"], usage.cost_microusd if usage else 0) if unknown else usage.cost_microusd
            overrun = bool(usage and (usage.total_tokens > row["quoted_tokens"] or usage.cost_microusd > row["quoted_cost"]))
            state = "overrun" if overrun else "failed" if unknown else "complete"
            if overrun:
                connection.execute("UPDATE tenants SET frozen=1 WHERE tenant=?", (reservation.tenant_id,))
            connection.execute("UPDATE reservations SET status=?,charged_tokens=?,charged_cost=?,response=?,settlement_hash=? WHERE tenant=? AND request=? AND call=?",
                               (state, tokens, cost, json.dumps(payload["response"]) if response else None,
                                digest, reservation.tenant_id, reservation.request_id, reservation.call_id))
            row = connection.execute("SELECT * FROM reservations WHERE tenant=? AND request=? AND call=?",
                                     (reservation.tenant_id, reservation.request_id, reservation.call_id)).fetchone()
            return self._reservation(row)

    def snapshot(self, tenant_id):
        with closing(self._connect()) as connection:
            connection.execute("BEGIN")
            policy = connection.execute("SELECT * FROM tenants WHERE tenant=?", (tenant_id,)).fetchone()
            if policy is None:
                raise AdmissionDenied("unknown_tenant")
            rows = connection.execute("SELECT status,charged_tokens,charged_cost FROM reservations WHERE tenant=?", (tenant_id,)).fetchall()
            return {"tenant_id": tenant_id, "token_limit": policy["token_limit"],
                    "cost_limit_microusd": policy["cost_limit"], "frozen": bool(policy["frozen"]),
                    "charged_tokens": sum(row["charged_tokens"] for row in rows),
                    "charged_microusd": sum(row["charged_cost"] for row in rows),
                    "reserved_tokens": sum(row["charged_tokens"] for row in rows if row["status"] == "reserved"),
                    "reservation_count": len(rows)}
