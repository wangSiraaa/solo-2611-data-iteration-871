"""中心额度账本与分站离线额度单日志（SQLite 持久化）。

角色与合同
==========
**中心 ``QuotaLedger``**
  持有按池（``pool_id``）与结算周期（``period``）独占划出的额度单 ``ticket``。
  一张额度单只绑定**一个池 + 一个周期 + 一个分站 + 一类资源**；划单即从该池
  的“可再分配”余额里独占预留，共享池经别名划出的两张单共同受同一池限额约束。

**分站 ``StationJournal``（断网工作）**
  分站离线时只持有中心已经独占划给本站的额度单快照。离线承诺必须在
  **本单内同时承诺工时与托位两种资源**：任一单余额不足 => 两边都不扣，
  生成本地可复核拒收回执；成功才同时扣减并落一条 pending 承诺。

**重联 ``sync_pending``（幂等）**
  每个承诺凭 ``request_id`` 全局只结算一次：重复回传返回原结算结果，绝不双扣；
  额度单在结算后被关闭（未用额度退回中心、重新可再分配），关闭单/过期单上的
  新离线承诺一律拒收并落回执。

周结三笔量（按池）：``settled`` 已结算、``allocated_unused`` 已划未用、
``reallocatable`` 可再分配，恒满足
``限额 == 已结算 + 已划未用 + 可再分配``。
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from .errors import OfflineRejected, QuotaOverAllocated, TicketError
from .quota import PoolRegistry, RESOURCES


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _new_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:12]}"


# --------------------------------------------------------------------------- #
# 数据传输对象
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class Ticket:
    ticket_id: str
    pool_id: str
    resource: str
    period: str
    station: str
    amount: int          # 独占划出的额度
    used: int            # 已被离线承诺占用（回传结算后入账）
    status: str          # open | closed | expired
    product: str
    version: int
    created_at: str
    closed_at: str | None

    @property
    def available(self) -> int:
        return self.amount - self.used


@dataclass(frozen=True)
class Promise:
    request_id: str
    station: str
    period: str
    hours_ticket: str
    pallets_ticket: str
    hours_need: int
    pallets_need: int
    task_id: str | None
    status: str          # pending | settled
    created_at: str
    settled_at: str | None


@dataclass(frozen=True)
class Receipt:
    """可复核拒收/受理回执：离线现场与中心重联都落同结构记录。"""

    receipt_id: str
    created_at: str
    station: str
    request_id: str | None
    reason: str
    detail: str
    context: dict

    def as_dict(self) -> dict:
        return {
            "receipt_id": self.receipt_id,
            "created_at": self.created_at,
            "station": self.station,
            "request_id": self.request_id,
            "reason": self.reason,
            "detail": self.detail,
            "context": self.context,
        }


@dataclass(frozen=True)
class SettleResult:
    request_id: str
    duplicated: bool
    hours_ticket: str
    pallets_ticket: str
    hours_need: int
    pallets_need: int
    status: str


# --------------------------------------------------------------------------- #
# 中心账本
# --------------------------------------------------------------------------- #
_LEDGER_SCHEMA = """
CREATE TABLE IF NOT EXISTS ledger_registry (
    period       TEXT PRIMARY KEY,
    product      TEXT NOT NULL,
    version      INTEGER NOT NULL,
    doc_name     TEXT NOT NULL,
    registry_json TEXT NOT NULL,
    created_at   TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS ticket (
    ticket_id   TEXT PRIMARY KEY,
    pool_id     TEXT NOT NULL,
    resource    TEXT NOT NULL,
    period      TEXT NOT NULL,
    station     TEXT NOT NULL,
    amount      INTEGER NOT NULL,
    used        INTEGER NOT NULL DEFAULT 0,
    status      TEXT NOT NULL DEFAULT 'open',
    product     TEXT NOT NULL,
    version     INTEGER NOT NULL,
    created_at  TEXT NOT NULL,
    closed_at   TEXT
);
CREATE TABLE IF NOT EXISTS promise (
    request_id     TEXT PRIMARY KEY,
    station        TEXT NOT NULL,
    period         TEXT NOT NULL,
    hours_ticket   TEXT NOT NULL,
    pallets_ticket TEXT NOT NULL,
    hours_need     INTEGER NOT NULL,
    pallets_need   INTEGER NOT NULL,
    task_id        TEXT,
    status         TEXT NOT NULL DEFAULT 'settled',
    created_at     TEXT NOT NULL,
    settled_at     TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS receipt (
    receipt_id  TEXT PRIMARY KEY,
    created_at  TEXT NOT NULL,
    station     TEXT NOT NULL,
    request_id  TEXT,
    reason      TEXT NOT NULL,
    detail      TEXT NOT NULL,
    context_json TEXT NOT NULL
);
"""


class QuotaLedger:
    """中心额度账本。线程模型：单连接串行使用。"""

    def __init__(self, db_path: str | Path):
        self.db_path = str(db_path)
        self.conn = sqlite3.connect(self.db_path)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.executescript(_LEDGER_SCHEMA)
        self.conn.commit()
        self._registry_cache: dict[str, PoolRegistry] = {}

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> "QuotaLedger":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # ------------------------------------------------------------------ #
    # 周期注册表（池身份目录的落库快照）
    # ------------------------------------------------------------------ #
    def register_period(self, registry: PoolRegistry) -> None:
        """登记某结算周期生效的池目录快照（重开后仍可按周期复核）。"""
        payload = json.dumps(registry.as_dict(), ensure_ascii=False)
        self.conn.execute(
            "INSERT OR REPLACE INTO ledger_registry(period, product, version, doc_name,"
            " registry_json, created_at) VALUES(?, ?, ?, ?, ?, ?)",
            (registry.period, registry.product, registry.version, registry.doc_name, payload, _now()),
        )
        self.conn.commit()
        self._registry_cache[registry.period] = registry

    def registry_for(self, period: str) -> PoolRegistry:
        if period in self._registry_cache:
            return self._registry_cache[period]
        row = self.conn.execute(
            "SELECT registry_json FROM ledger_registry WHERE period = ?", (period,)
        ).fetchone()
        if row is None:
            raise TicketError(f"周期 {period!r} 未登记池目录")
        registry = _registry_from_dict(json.loads(row["registry_json"]))
        self._registry_cache[period] = registry
        return registry

    # ------------------------------------------------------------------ #
    # 划单：中心独占预留（共享池受同一限额约束）
    # ------------------------------------------------------------------ #
    def issue_ticket(
        self,
        station: str,
        resource: str,
        amount: int,
        period: str,
        registry: PoolRegistry | None = None,
    ) -> Ticket:
        registry = registry or self.registry_for(period)
        if registry.period != period:
            raise TicketError(f"注册表周期 {registry.period!r} 与划单周期 {period!r} 不一致")
        if resource not in RESOURCES:
            raise TicketError(f"未知资源 {resource!r}")
        if amount < 0:
            raise TicketError("划单额度不能为负")
        pool = registry.pool_for(station, resource)
        return self._issue(pool.pool_id, resource, period, station, amount, registry)

    def _issue(self, pool_id, resource, period, station, amount, registry) -> Ticket:
        with self.conn:  # 单事务：余额检查与插入原子，超额整体失败
            row = self.conn.execute(
                "SELECT COALESCE(SUM(amount - used), 0) AS reserved FROM ticket"
                " WHERE pool_id = ? AND period = ? AND status = 'open'",
                (pool_id, period),
            ).fetchone()
            limit = registry.pools[pool_id].limit
            if row["reserved"] + amount > limit:
                raise QuotaOverAllocated(
                    f"池 {pool_id}（限额 {limit}）已划 {row['reserved']}，再划 {amount} 超额；"
                    f"可再分配仅 {limit - row['reserved']}"
                )
            ticket_id = _new_id("tkt")
            created = _now()
            self.conn.execute(
                "INSERT INTO ticket(ticket_id, pool_id, resource, period, station, amount,"
                " used, status, product, version, created_at) VALUES(?, ?, ?, ?, ?, ?, 0,"
                " 'open', ?, ?, ?)",
                (ticket_id, pool_id, resource, period, station, amount, registry.product,
                 registry.version, created),
            )
        return self.get_ticket(ticket_id)

    # ------------------------------------------------------------------ #
    def get_ticket(self, ticket_id: str) -> Ticket:
        row = self.conn.execute("SELECT * FROM ticket WHERE ticket_id = ?", (ticket_id,)).fetchone()
        if row is None:
            raise TicketError(f"额度单 {ticket_id!r} 不存在")
        return _ticket_from_row(row)

    def open_tickets(self, station: str, period: str) -> list[Ticket]:
        rows = self.conn.execute(
            "SELECT * FROM ticket WHERE station = ? AND period = ? AND status = 'open'"
            " ORDER BY resource, ticket_id",
            (station, period),
        ).fetchall()
        return [_ticket_from_row(r) for r in rows]

    def ticket_snapshot_for(self, station: str, period: str) -> dict[str, Ticket]:
        """分站离线前拉取的本站本周期额度单快照：resource -> Ticket。"""
        return {t.resource: t for t in self.open_tickets(station, period)}

    # ------------------------------------------------------------------ #
    # 重联结算：幂等，每个 request_id 只扣一次
    # ------------------------------------------------------------------ #
    def settle_promise(self, promise: dict) -> SettleResult:
        """结算一条分站回传承诺。重复 request_id => 幂等返回原结果，绝不双扣。"""
        request_id = promise["request_id"]
        dup = self.conn.execute(
            "SELECT * FROM promise WHERE request_id = ?", (request_id,)
        ).fetchone()
        if dup is not None:
            return SettleResult(
                request_id=request_id,
                duplicated=True,
                hours_ticket=dup["hours_ticket"],
                pallets_ticket=dup["pallets_ticket"],
                hours_need=dup["hours_need"],
                pallets_need=dup["pallets_need"],
                status=dup["status"],
            )

        station = promise["station"]
        period = promise["period"]
        h_ticket_id = promise["hours_ticket"]
        p_ticket_id = promise["pallets_ticket"]
        h_need = int(promise["hours_need"])
        p_need = int(promise["pallets_need"])
        h_ticket = self.get_ticket(h_ticket_id)
        p_ticket = self.get_ticket(p_ticket_id)

        # 中心侧重验：单必须属于该站、本周期、仍 open、且绑定两类不同资源
        for t, need, resource in (
            (h_ticket, h_need, "hours"),
            (p_ticket, p_need, "pallets"),
        ):
            if t.station != station:
                raise TicketError(f"{resource} 单不属于分站 {station!r}")
            if t.period != period:
                raise TicketError(f"{resource} 单周期 {t.period!r} 与承诺周期 {period!r} 不符")
            if t.status != "open":
                raise TicketError(f"{resource} 单 {t.ticket_id} 已 {t.status}，不可再结算")
            if t.resource != resource:
                raise TicketError(f"额度单 {t.ticket_id} 资源类型 {t.resource!r} 不是 {resource!r}")
            if need > t.available:
                raise TicketError(
                    f"{resource} 单 {t.ticket_id} 可用 {t.available} 不足以承诺 {need}"
                )

        with self.conn:  # 两种资源同一事务同时入账，任一失败整体回滚
            self.conn.execute(
                "UPDATE ticket SET used = used + ? WHERE ticket_id = ?", (h_need, h_ticket_id)
            )
            self.conn.execute(
                "UPDATE ticket SET used = used + ? WHERE ticket_id = ?", (p_need, p_ticket_id)
            )
            self.conn.execute(
                "INSERT INTO promise(request_id, station, period, hours_ticket, pallets_ticket,"
                " hours_need, pallets_need, task_id, status, created_at, settled_at)"
                " VALUES(?, ?, ?, ?, ?, ?, ?, ?, 'settled', ?, ?)",
                (
                    request_id, station, period, h_ticket_id, p_ticket_id, h_need, p_need,
                    promise.get("task_id"), promise.get("created_at", _now()), _now(),
                ),
            )
        return SettleResult(
            request_id=request_id,
            duplicated=False,
            hours_ticket=h_ticket_id,
            pallets_ticket=p_ticket_id,
            hours_need=h_need,
            pallets_need=p_need,
            status="settled",
        )

    def sync_pending(self, station: str, promises: list[dict]) -> list[SettleResult]:
        """整批回传：逐条幂等结算；某条因单关闭/过期被拒时落中心回执后继续。"""
        results: list[SettleResult] = []
        for p in promises:
            try:
                results.append(self.settle_promise(p))
            except TicketError as exc:
                self.add_receipt(
                    station=station,
                    reason="rejected-on-sync",
                    detail=str(exc),
                    context={"request_id": p.get("request_id"), "promise": p},
                    request_id=p.get("request_id"),
                )
        return results

    # ------------------------------------------------------------------ #
    # 关单：退回未用额度（重新可再分配），关闭后新承诺拒收
    # ------------------------------------------------------------------ #
    def close_ticket(self, ticket_id: str) -> Ticket:
        ticket = self.get_ticket(ticket_id)
        if ticket.status != "open":
            raise TicketError(f"额度单 {ticket_id} 已是 {ticket.status}")
        with self.conn:
            self.conn.execute(
                "UPDATE ticket SET status = 'closed', closed_at = ? WHERE ticket_id = ?",
                (_now(), ticket_id),
            )
        return self.get_ticket(ticket_id)

    def expire_period(self, period: str) -> list[Ticket]:
        """周期过期：本周期所有仍 open 的单标记 expired（未用量自然回到可再分配口径）。"""
        rows = self.conn.execute(
            "SELECT ticket_id FROM ticket WHERE period = ? AND status = 'open'", (period,)
        ).fetchall()
        expired = []
        for r in rows:
            with self.conn:
                self.conn.execute(
                    "UPDATE ticket SET status = 'expired', closed_at = ? WHERE ticket_id = ?",
                    (_now(), r["ticket_id"]),
                )
            expired.append(self.get_ticket(r["ticket_id"]))
        return expired

    # ------------------------------------------------------------------ #
    def add_receipt(
        self,
        station: str,
        reason: str,
        detail: str,
        context: dict,
        request_id: str | None = None,
    ) -> Receipt:
        receipt = Receipt(
            receipt_id=_new_id("rcp"),
            created_at=_now(),
            station=station,
            request_id=request_id,
            reason=reason,
            detail=detail,
            context=context,
        )
        self.conn.execute(
            "INSERT INTO receipt(receipt_id, created_at, station, request_id, reason, detail,"
            " context_json) VALUES(?, ?, ?, ?, ?, ?, ?)",
            (
                receipt.receipt_id, receipt.created_at, receipt.station, receipt.request_id,
                receipt.reason, receipt.detail, json.dumps(context, ensure_ascii=False),
            ),
        )
        self.conn.commit()
        return receipt

    def list_receipts(self, station: str | None = None) -> list[dict]:
        if station is None:
            rows = self.conn.execute("SELECT * FROM receipt ORDER BY created_at").fetchall()
        else:
            rows = self.conn.execute(
                "SELECT * FROM receipt WHERE station = ? ORDER BY created_at", (station,)
            ).fetchall()
        return [
            {
                "receipt_id": r["receipt_id"],
                "created_at": r["created_at"],
                "station": r["station"],
                "request_id": r["request_id"],
                "reason": r["reason"],
                "detail": r["detail"],
                "context": json.loads(r["context_json"]),
            }
            for r in rows
        ]

    def list_promises(self, station: str | None = None) -> list[dict]:
        if station is None:
            rows = self.conn.execute("SELECT * FROM promise ORDER BY settled_at").fetchall()
        else:
            rows = self.conn.execute(
                "SELECT * FROM promise WHERE station = ? ORDER BY settled_at", (station,)
            ).fetchall()
        return [dict(r) for r in rows]

    # ------------------------------------------------------------------ #
    # 周结：按池复核三笔量
    # ------------------------------------------------------------------ #
    def weekly_reconciliation(self, period: str) -> list[dict]:
        """按池产出三笔量：已结算 / 已划未用 / 可再分配。

        * settled          —— 已结算承诺实际占用（ticket.used 之和）；
        * allocated_unused —— 仍 open 单里划出未用（amount-used），
                              closed/expired 单的未用部分已退回，不在此列；
        * reallocatable    —— 限额减去上述两笔，即中心可再分配余额。
        """
        registry = self.registry_for(period)
        report = []
        for pool_id in sorted(registry.pools):
            pool = registry.pools[pool_id]
            agg = self.conn.execute(
                "SELECT COALESCE(SUM(used), 0) AS settled,"
                " COALESCE(SUM(CASE WHEN status = 'open' THEN amount - used ELSE 0 END), 0)"
                "   AS allocated_unused,"
                " COALESCE(SUM(amount), 0) AS issued_total,"
                " COUNT(*) AS ticket_count"
                " FROM ticket WHERE pool_id = ? AND period = ?",
                (pool_id, period),
            ).fetchone()
            settled = int(agg["settled"])
            allocated_unused = int(agg["allocated_unused"])
            reallocatable = pool.limit - settled - allocated_unused
            assert reallocatable >= 0, "三笔量不可能超过限额"
            report.append(
                {
                    "pool_id": pool_id,
                    "name": pool.name,
                    "renamed_from": pool.renamed_from,
                    "resource": pool.resource,
                    "period": period,
                    "limit": pool.limit,
                    "settled": settled,
                    "allocated_unused": allocated_unused,
                    "reallocatable": reallocatable,
                    "ticket_count": int(agg["ticket_count"]),
                    "identity_invariant_holds": (
                        settled + allocated_unused + reallocatable == pool.limit
                    ),
                }
            )
        return report


# --------------------------------------------------------------------------- #
# 分站离线日志
# --------------------------------------------------------------------------- #
_STATION_SCHEMA = """
CREATE TABLE IF NOT EXISTS station_ticket (
    ticket_id   TEXT PRIMARY KEY,
    pool_id     TEXT NOT NULL,
    resource    TEXT NOT NULL,
    period      TEXT NOT NULL,
    amount      INTEGER NOT NULL,
    used        INTEGER NOT NULL DEFAULT 0,
    status      TEXT NOT NULL DEFAULT 'open',
    pulled_at   TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS station_promise (
    request_id     TEXT PRIMARY KEY,
    period         TEXT NOT NULL,
    hours_ticket   TEXT NOT NULL,
    pallets_ticket TEXT NOT NULL,
    hours_need     INTEGER NOT NULL,
    pallets_need   INTEGER NOT NULL,
    task_id        TEXT,
    status         TEXT NOT NULL DEFAULT 'pending',
    created_at     TEXT NOT NULL,
    synced_at      TEXT
);
CREATE TABLE IF NOT EXISTS station_receipt (
    receipt_id   TEXT PRIMARY KEY,
    created_at   TEXT NOT NULL,
    request_id   TEXT,
    reason       TEXT NOT NULL,
    detail       TEXT NOT NULL,
    context_json TEXT NOT NULL
);
"""


class StationJournal:
    """分站离线日志：只认识中心划给本站、已拉到本地的额度单。"""

    def __init__(self, db_path: str | Path, station: str, current_period: str):
        self.db_path = str(db_path)
        self.station = station
        self.current_period = current_period
        self.conn = sqlite3.connect(self.db_path)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(_STATION_SCHEMA)
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> "StationJournal":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def pull_tickets(self, tickets: list[Ticket]) -> None:
        """离线前从中心拉取本站额度单快照（独占事实以中心划单为准）。"""
        with self.conn:
            for t in tickets:
                if t.station != self.station:
                    raise TicketError(f"额度单 {t.ticket_id} 不属于本站 {self.station!r}")
                self.conn.execute(
                    "INSERT OR REPLACE INTO station_ticket(ticket_id, pool_id, resource, period,"
                    " amount, used, status, pulled_at) VALUES(?, ?, ?, ?, ?, ?, ?, ?)",
                    (t.ticket_id, t.pool_id, t.resource, t.period, t.amount, t.used, t.status, _now()),
                )

    def _local_ticket(self, ticket_id: str) -> sqlite3.Row:
        row = self.conn.execute(
            "SELECT * FROM station_ticket WHERE ticket_id = ?", (ticket_id,)
        ).fetchone()
        if row is None:
            raise TicketError(f"本地无额度单 {ticket_id!r}（中心未划给本站或未拉取）")
        return row

    def local_tickets(self) -> list[dict]:
        rows = self.conn.execute("SELECT * FROM station_ticket ORDER BY resource").fetchall()
        return [dict(r) for r in rows]

    # ------------------------------------------------------------------ #
    def offline_promise(
        self,
        hours_need: int,
        pallets_need: int,
        task_id: str | None = None,
        request_id: str | None = None,
    ) -> Promise:
        """离线承诺：在本单内**同时**承诺工时与托位。

        任一不足 => 两种资源都不扣，落可复核回执并抛 :class:`OfflineRejected`；
        成功 => 两单同事务扣减，落 pending 承诺等待重联结算。
        """
        request_id = request_id or _new_id("req")
        if hours_need < 0 or pallets_need < 0:
            raise OfflineRejected("承诺量不能为负", reason="negative-need")
        try:
            h_rows = self.conn.execute(
                "SELECT * FROM station_ticket WHERE resource = 'hours' AND status = 'open'"
                " ORDER BY pulled_at, ticket_id"
            ).fetchall()
            p_rows = self.conn.execute(
                "SELECT * FROM station_ticket WHERE resource = 'pallets' AND status = 'open'"
                " ORDER BY pulled_at, ticket_id"
            ).fetchall()
            if not h_rows or not p_rows:
                raise OfflineRejected(
                    "本站缺少 open 的工时单或托位单，无法同时承诺两种资源",
                    reason="ticket-unavailable",
                    context={"hours_tickets": len(h_rows), "pallets_tickets": len(p_rows)},
                )
            # 周期校验：离线单都绑定周期，过期周期的单不得再承诺
            if any(r["period"] != self.current_period for r in h_rows + p_rows):
                raise OfflineRejected(
                    "存在与当前结算周期不符的额度单（额度单已过期）",
                    reason="ticket-expired",
                    context={
                        "current_period": self.current_period,
                        "ticket_periods": sorted({r["period"] for r in h_rows + p_rows}),
                    },
                )
            # 补足后可能有多张同类 open 单：选最早一张余额足以覆盖需求的单；
            # 没有任何单够 => 整单不扣（绝不跨单拼凑成部分承诺）
            h_row = next((r for r in h_rows if r["amount"] - r["used"] >= hours_need), None)
            p_row = next((r for r in p_rows if r["amount"] - r["used"] >= pallets_need), None)
            h_avail = h_rows[0]["amount"] - h_rows[0]["used"]
            p_avail = p_rows[0]["amount"] - p_rows[0]["used"]
            if h_row is None or p_row is None:
                raise OfflineRejected(
                    f"额度不足：工时需 {hours_need}/最大余 "
                    f"{max(r['amount'] - r['used'] for r in h_rows)}，"
                    f"托位需 {pallets_need}/最大余 "
                    f"{max(r['amount'] - r['used'] for r in p_rows)}；两种资源均不扣",
                    reason="insufficient-balance",
                    context={
                        "hours_need": hours_need,
                        "hours_available_first": h_avail,
                        "pallets_need": pallets_need,
                        "pallets_available_first": p_avail,
                        "hours_ticket": h_rows[0]["ticket_id"],
                        "pallets_ticket": p_rows[0]["ticket_id"],
                    },
                )
            h_avail, p_avail = h_row["amount"] - h_row["used"], p_row["amount"] - p_row["used"]

            with self.conn:  # 原子：两单同时扣，任一异常整体回滚
                self.conn.execute(
                    "UPDATE station_ticket SET used = used + ? WHERE ticket_id = ?",
                    (hours_need, h_row["ticket_id"]),
                )
                self.conn.execute(
                    "UPDATE station_ticket SET used = used + ? WHERE ticket_id = ?",
                    (pallets_need, p_row["ticket_id"]),
                )
                created = _now()
                self.conn.execute(
                    "INSERT INTO station_promise(request_id, period, hours_ticket, pallets_ticket,"
                    " hours_need, pallets_need, task_id, status, created_at)"
                    " VALUES(?, ?, ?, ?, ?, ?, ?, 'pending', ?)",
                    (
                        request_id, self.current_period, h_row["ticket_id"], p_row["ticket_id"],
                        hours_need, pallets_need, task_id, created,
                    ),
                )
            return Promise(
                request_id=request_id,
                station=self.station,
                period=self.current_period,
                hours_ticket=h_row["ticket_id"],
                pallets_ticket=p_row["ticket_id"],
                hours_need=hours_need,
                pallets_need=pallets_need,
                task_id=task_id,
                status="pending",
                created_at=created,
                settled_at=None,
            )
        except OfflineRejected as rejected:
            self._record_rejection(request_id, rejected, hours_need, pallets_need, task_id)
            raise

    def _record_rejection(self, request_id, rejected: OfflineRejected, h_need, p_need, task_id) -> None:
        context = dict(rejected.context)
        context.update(
            {
                "station": self.station,
                "hours_need": h_need,
                "pallets_need": p_need,
                "task_id": task_id,
            }
        )
        with self.conn:
            self.conn.execute(
                "INSERT INTO station_receipt(receipt_id, created_at, request_id, reason, detail,"
                " context_json) VALUES(?, ?, ?, ?, ?, ?)",
                (_new_id("rcp"), _now(), request_id, rejected.reason, str(rejected),
                 json.dumps(context, ensure_ascii=False)),
            )

    # ------------------------------------------------------------------ #
    def pending_promises(self) -> list[dict]:
        rows = self.conn.execute(
            "SELECT * FROM station_promise WHERE status = 'pending' ORDER BY created_at"
        ).fetchall()
        return [dict(r) for r in rows]

    def mark_synced(self, request_ids: list[str]) -> None:
        with self.conn:
            for rid in request_ids:
                self.conn.execute(
                    "UPDATE station_promise SET status = 'settled', synced_at = ?"
                    " WHERE request_id = ?",
                    (_now(), rid),
                )

    def local_receipts(self) -> list[dict]:
        rows = self.conn.execute("SELECT * FROM station_receipt ORDER BY created_at").fetchall()
        return [
            {
                "receipt_id": r["receipt_id"],
                "created_at": r["created_at"],
                "request_id": r["request_id"],
                "reason": r["reason"],
                "detail": r["detail"],
                "context": json.loads(r["context_json"]),
            }
            for r in rows
        ]

    def drain_for_sync(self, ledger: QuotaLedger) -> list[SettleResult]:
        """重联：把本地 pending 承诺回传中心幂等结算，并回写本地状态。"""
        pending = self.pending_promises()
        payloads = [
            {
                "request_id": p["request_id"],
                "station": self.station,
                "period": p["period"],
                "hours_ticket": p["hours_ticket"],
                "pallets_ticket": p["pallets_ticket"],
                "hours_need": p["hours_need"],
                "pallets_need": p["pallets_need"],
                "task_id": p["task_id"],
                "created_at": p["created_at"],
            }
            for p in pending
        ]
        results = ledger.sync_pending(self.station, payloads)
        self.mark_synced([r.request_id for r in results])
        return results


# --------------------------------------------------------------------------- #
# 序列化辅助
# --------------------------------------------------------------------------- #
def _ticket_from_row(row: sqlite3.Row) -> Ticket:
    return Ticket(
        ticket_id=row["ticket_id"],
        pool_id=row["pool_id"],
        resource=row["resource"],
        period=row["period"],
        station=row["station"],
        amount=row["amount"],
        used=row["used"],
        status=row["status"],
        product=row["product"],
        version=row["version"],
        created_at=row["created_at"],
        closed_at=row["closed_at"],
    )


def _registry_from_dict(data: dict) -> PoolRegistry:
    """从周结落库 JSON 重建精简注册表（只保留复核所需身份/限额/周期字段）。"""
    from .quota import PoolRecord, PoolView

    pools: dict[str, PoolRecord] = {}
    effective: dict[tuple[str, str], str] = {}
    for pd in data["pools"]:
        views = [
            PoolView(
                station=v["station"],
                resource=pd["resource"],
                limit=v["limit"],
                via_anchor=v["via_anchor"],
                alias_chain=tuple(v["alias_chain"]),
                provenance=v["provenance"],
                pool_id=pd["pool_id"],
            )
            for v in pd["views"]
        ]
        pools[pd["pool_id"]] = PoolRecord(
            pool_id=pd["pool_id"],
            resource=pd["resource"],
            period=pd["period"],
            name=pd["name"],
            warehouse=pd["warehouse"],
            limit=pd["limit"],
            renamed_from=pd.get("renamed_from"),
            views=views,
        )
    for e in data["effective"]:
        effective[(e["station"], e["resource"])] = e["pool_id"]
    return PoolRegistry(
        product=data["product"],
        version=data["version"],
        doc_name=data["doc_name"],
        period=data["period"],
        pools=pools,
        effective=effective,
    )
