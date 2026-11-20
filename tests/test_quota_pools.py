"""额度池身份 / 离线额度单合同测试。

覆盖题面全部硬性规则：
1. 别名/merge 同锚点 => 同池；同周期两别名各显 5 是同一池，不能各领 3；
2. 同值显式独立池 => 不同 pool_id，不合并，可以各领 3；
3. 池重命名身份不变，旧发放仍占原余额；同池读法限额冲突即发布期拒绝；
4. 离线同时承诺两种资源，任一不足两边都不扣（原子）+ 可复核回执；
5. 重联幂等：同一 request_id 只结算一次，重复回传不双扣；
6. 关单退回未用后，关闭/过期单的新承诺拒收并给回执；
7. 周结三笔量恒等式：限额 == 已结算 + 已划未用 + 可再分配；
8. 换算约定从真实 CSV 明细算需求，重开结果一致。
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from confcat.catalog import Catalog
from confcat.demand import compute_batch_demand, demand_for_task, load_conversion, ConversionRule
from confcat.errors import (
    OfflineRejected,
    QuotaConfigError,
    QuotaOverAllocated,
    TicketError,
)
from confcat.ingest import TaskRepository
from confcat.quota import apply_rename, registry_from_catalog
from confcat.quota_ledger import QuotaLedger, StationJournal

# 两个分站的工时经别名指向同一锚点（共 5）；托位是各 4 的独立池
SHARED_YAML = """
quota:
  period: "2026-W47"
  shared_hours: &sh
    name: shared-hours
    warehouse: WH
    limit: 5
  station-a:
    hours: *sh
    pallets:
      name: pa
      limit: 4
  station-b:
    hours:
      <<: *sh
    pallets:
      name: pb
      limit: 4
conversion:
  key_column: channel
  defaults:
    hours_per_row: 1
    pallets_per_row: 1
  rules:
    web:
      hours_per_row: 2
      pallets_per_row: 2
ingest:
  source_uri: file:///t.csv
  column_mapping:
    task_ref: oid
    channel: chan
"""

# 两个分站都是各 5 的显式独立池（同值但无别名链）
INDEPENDENT_YAML = """
quota:
  period: "2026-W47"
  station-a:
    hours:
      name: ha
      limit: 5
    pallets:
      name: pa
      limit: 5
  station-b:
    hours:
      name: hb
      limit: 5
    pallets:
      name: pb
      limit: 5
conversion:
  key_column: channel
  defaults:
    hours_per_row: 1
    pallets_per_row: 1
ingest:
  source_uri: file:///t.csv
  column_mapping:
    task_ref: oid
    channel: chan
"""

CSV = "oid,chan\n1,web\n2,app\n3,web\n"


class PoolIdentityTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        d = Path(self.tmp.name)
        self.cat = Catalog(d / "cat.db")
        self.ledger_db = d / "ledger.db"
        self.cat.publish("shared", SHARED_YAML)
        self.cat.publish("indep", INDEPENDENT_YAML)

    def tearDown(self):
        self.cat.close()
        self.tmp.cleanup()

    def test_alias_and_merge_views_resolve_to_same_pool(self):
        reg = registry_from_catalog(self.cat, "shared", "doc-0")
        pa = reg.pool_for("station-a", "hours")
        pb = reg.pool_for("station-b", "hours")
        self.assertEqual(pa.pool_id, pb.pool_id)
        self.assertEqual(len(pa.views), 2)
        # 直接别名与 merge 读法都归属同一锚点身份（merge 视图本身无别名链也仍同池）
        self.assertEqual({v.station for v in pa.views}, {"station-a", "station-b"})
        self.assertTrue(all(v.via_anchor == "sh" for v in pa.views))
        self.assertTrue(any(v.alias_chain == ("sh",) for v in pa.views))

    def test_equal_value_explicit_pools_are_not_merged(self):
        reg = registry_from_catalog(self.cat, "indep", "doc-0")
        pa = reg.pool_for("station-a", "hours")
        pb = reg.pool_for("station-b", "hours")
        self.assertNotEqual(pa.pool_id, pb.pool_id)
        self.assertEqual(pa.limit, pb.limit)
        self.assertIsNone(pa.views[0].via_anchor)

    def test_shared_pool_cannot_issue_three_to_each_station(self):
        reg = registry_from_catalog(self.cat, "shared", "doc-0")
        with QuotaLedger(self.ledger_db) as ledger:
            ledger.register_period(reg)
            ledger.issue_ticket("station-a", "hours", 3, reg.period, reg)
            with self.assertRaises(QuotaOverAllocated):
                ledger.issue_ticket("station-b", "hours", 3, reg.period, reg)
            # 改成 2 恰好占满
            ledger.issue_ticket("station-b", "hours", 2, reg.period, reg)

    def test_independent_pools_allow_three_each(self):
        reg = registry_from_catalog(self.cat, "indep", "doc-0")
        with QuotaLedger(self.ledger_db) as ledger:
            ledger.register_period(reg)
            ledger.issue_ticket("station-a", "hours", 3, reg.period, reg)
            ledger.issue_ticket("station-b", "hours", 3, reg.period, reg)  # 不抛
            free = reg.pools[reg.pool_for("station-b", "hours").pool_id].limit - 3
            self.assertEqual(free, 2)

    def test_rename_keeps_identity_and_old_ticket_balance(self):
        reg = registry_from_catalog(self.cat, "shared", "doc-0")
        with QuotaLedger(self.ledger_db) as ledger:
            ledger.register_period(reg)
            t = ledger.issue_ticket("station-a", "hours", 3, reg.period, reg)
            renamed = SHARED_YAML.replace("name: shared-hours", "name: shared-hours-v2")
            self.cat.publish("shared", renamed)
            reg2 = apply_rename(reg, registry_from_catalog(self.cat, "shared", "doc-0"))
            pool2 = reg2.pool_for("station-a", "hours")
            self.assertEqual(pool2.pool_id, t.pool_id)
            self.assertEqual(pool2.renamed_from, "shared-hours")
            self.assertEqual(ledger.get_ticket(t.ticket_id).used, 0)

    def test_conflicting_reads_of_same_anchor_rejected_at_publish(self):
        bad = SHARED_YAML.replace(
            "  station-b:\n    hours:\n      <<: *sh",
            "  station-b:\n    hours:\n      <<: *sh\n      limit: 9",
        )
        self.cat.publish("bad", bad)
        with self.assertRaises(QuotaConfigError) as ctx:
            registry_from_catalog(self.cat, "bad", "doc-0")
        self.assertEqual(ctx.exception.reason, "pool-identity-conflict")

    def test_missing_resource_declaration_rejected(self):
        bad = SHARED_YAML.replace("    pallets:\n      name: pa\n      limit: 4", "    note: x")
        self.cat.publish("bad2", bad)
        with self.assertRaises(QuotaConfigError):
            registry_from_catalog(self.cat, "bad2", "doc-0")


class OfflineAtomicityTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        d = Path(self.tmp.name)
        self.cat = Catalog(d / "cat.db")
        self.cat.publish("shared", SHARED_YAML)
        self.reg = registry_from_catalog(self.cat, "shared", "doc-0")
        self.ledger = QuotaLedger(d / "ledger.db")
        self.ledger.register_period(self.reg)
        self.journal_db = d / "station.db"

    def tearDown(self):
        self.ledger.close()
        self.cat.close()
        self.tmp.cleanup()

    def _journal(self, hours_amount: int, pallets_amount: int) -> StationJournal:
        th = self.ledger.issue_ticket("station-a", "hours", hours_amount, self.reg.period, self.reg)
        tp = self.ledger.issue_ticket("station-a", "pallets", pallets_amount, self.reg.period, self.reg)
        journal = StationJournal(self.journal_db, "station-a", self.reg.period)
        journal.pull_tickets([th, tp])
        return journal, th, tp

    def test_atomic_no_deduction_when_pallets_insufficient(self):
        """题面例：工时单 3 小时，任务需 2h/2 托；托位单仅 1 => 整单不扣。"""
        journal, th, tp = self._journal(3, 1)
        with self.assertRaises(OfflineRejected) as ctx:
            journal.offline_promise(2, 2, request_id="r1")
        self.assertEqual(ctx.exception.reason, "insufficient-balance")
        local = {t["resource"]: t["used"] for t in journal.local_tickets()}
        self.assertEqual(local, {"hours": 0, "pallets": 0})  # 两边都没扣
        receipt = journal.local_receipts()[0]
        self.assertEqual(receipt["reason"], "insufficient-balance")
        self.assertIn("pallets_available_first", receipt["context"])

    def test_retry_after_top_up_deducts_exactly_two_each(self):
        journal, th, tp = self._journal(3, 1)
        with self.assertRaises(OfflineRejected):
            journal.offline_promise(2, 2, request_id="r1")
        # 补足：再划一张 2 的托位单
        tp2 = self.ledger.issue_ticket("station-a", "pallets", 2, self.reg.period, self.reg)
        journal.pull_tickets([tp2])
        p = journal.offline_promise(2, 2, request_id="r2")
        self.assertEqual((p.hours_need, p.pallets_need), (2, 2))
        local = {t["resource"]: t["amount"] - t["used"] for t in journal.local_tickets()}
        # 工时余 1；第一张托位单仍 1 未动，第二张被扣 2 余 0
        self.assertEqual(local["hours"], 1)
        journal.close()

    def test_reconnect_settles_once_never_double_spend(self):
        journal, th, tp = self._journal(3, 3)
        p = journal.offline_promise(2, 2, request_id="r-x")
        results = journal.drain_for_sync(self.ledger)
        self.assertEqual(len(results), 1)
        self.assertFalse(results[0].duplicated)
        # 再传同一 request_id：幂等返回，不二次扣
        again = self.ledger.settle_promise({
            "request_id": "r-x", "station": "station-a", "period": self.reg.period,
            "hours_ticket": p.hours_ticket, "pallets_ticket": p.pallets_ticket,
            "hours_need": 2, "pallets_need": 2,
        })
        self.assertTrue(again.duplicated)
        self.assertEqual(self.ledger.get_ticket(p.hours_ticket).used, 2)
        self.assertEqual(self.ledger.get_ticket(p.pallets_ticket).used, 2)
        self.assertEqual(len(self.ledger.list_promises()), 1)
        journal.close()

    def test_closed_ticket_rejects_new_promise_with_receipt(self):
        journal, th, tp = self._journal(3, 3)
        p = journal.offline_promise(1, 1, request_id="r-ok")
        journal.drain_for_sync(self.ledger)
        closed = self.ledger.close_ticket(tp.ticket_id)
        self.assertEqual(closed.status, "closed")
        before = len(self.ledger.list_receipts())
        self.ledger.sync_pending("station-a", [{
            "request_id": "r-stale", "station": "station-a", "period": self.reg.period,
            "hours_ticket": th.ticket_id, "pallets_ticket": tp.ticket_id,
            "hours_need": 0, "pallets_need": 1,
        }])
        self.assertEqual(len(self.ledger.list_receipts()), before + 1)
        self.assertEqual(self.ledger.list_receipts()[-1]["reason"], "rejected-on-sync")
        journal.close()

    def test_expired_period_ticket_rejected_offline(self):
        old_yaml = SHARED_YAML.replace('"2026-W47"', '"2026-W46"')
        self.cat.publish("old", old_yaml)
        old_reg = registry_from_catalog(self.cat, "old", "doc-0")
        self.ledger.register_period(old_reg)
        th = self.ledger.issue_ticket("station-a", "hours", 2, "2026-W46", old_reg)
        tp = self.ledger.issue_ticket("station-a", "pallets", 2, "2026-W46", old_reg)
        journal = StationJournal(self.journal_db, "station-a", "2026-W47")  # 当前新周期
        journal.pull_tickets([th, tp])
        with self.assertRaises(OfflineRejected) as ctx:
            journal.offline_promise(1, 1, request_id="r-exp")
        self.assertEqual(ctx.exception.reason, "ticket-expired")
        journal.close()

    def test_center_revalidates_ticket_ownership(self):
        journal, th, tp = self._journal(3, 3)
        with self.assertRaises(TicketError):
            self.ledger.settle_promise({
                "request_id": "r-foreign", "station": "station-b", "period": self.reg.period,
                "hours_ticket": th.ticket_id, "pallets_ticket": tp.ticket_id,
                "hours_need": 1, "pallets_need": 1,
            })
        journal.close()

    def test_interleaved_cross_station_sync_settles_each_once(self):
        """两站交叉回传（B 先 A 后）+ 重复回传：每承诺仅结算一次，各扣各单。"""
        reg = self.reg
        ta_h = self.ledger.issue_ticket("station-a", "hours", 3, reg.period, reg)
        ta_p = self.ledger.issue_ticket("station-a", "pallets", 3, reg.period, reg)
        tb_h = self.ledger.issue_ticket("station-b", "hours", 2, reg.period, reg)
        tb_p = self.ledger.issue_ticket("station-b", "pallets", 3, reg.period, reg)
        ja = StationJournal(self.journal_db.parent / "a.db", "station-a", reg.period)
        jb = StationJournal(self.journal_db.parent / "b.db", "station-b", reg.period)
        ja.pull_tickets([ta_h, ta_p])
        jb.pull_tickets([tb_h, tb_p])
        pa = ja.offline_promise(2, 2, request_id="cross-A")
        pb = jb.offline_promise(1, 2, request_id="cross-B")
        # B 的先回传，A 的后回传
        rb = self.ledger.sync_pending("station-b", [{
            "request_id": pb.request_id, "station": "station-b", "period": reg.period,
            "hours_ticket": pb.hours_ticket, "pallets_ticket": pb.pallets_ticket,
            "hours_need": 1, "pallets_need": 2,
        }])
        ra = self.ledger.sync_pending("station-a", [{
            "request_id": pa.request_id, "station": "station-a", "period": reg.period,
            "hours_ticket": pa.hours_ticket, "pallets_ticket": pa.pallets_ticket,
            "hours_need": 2, "pallets_need": 2,
        }])
        self.assertEqual([r.duplicated for r in rb + ra], [False, False])
        # A 再重复回传：幂等，不双扣
        again = self.ledger.settle_promise({
            "request_id": pa.request_id, "station": "station-a", "period": reg.period,
            "hours_ticket": pa.hours_ticket, "pallets_ticket": pa.pallets_ticket,
            "hours_need": 2, "pallets_need": 2,
        })
        self.assertTrue(again.duplicated)
        self.assertEqual(self.ledger.get_ticket(ta_h.ticket_id).used, 2)
        self.assertEqual(self.ledger.get_ticket(tb_h.ticket_id).used, 1)
        self.assertEqual(len(self.ledger.list_promises()), 2)
        ja.close()
        jb.close()


class WeeklyReconciliationTest(unittest.TestCase):
    def test_three_way_invariant_per_pool(self):
        tmp = tempfile.TemporaryDirectory()
        d = Path(tmp.name)
        try:
            cat = Catalog(d / "cat.db")
            cat.publish("indep", INDEPENDENT_YAML)
            reg = registry_from_catalog(cat, "indep", "doc-0")
            with QuotaLedger(d / "l.db") as ledger:
                ledger.register_period(reg)
                th = ledger.issue_ticket("station-a", "hours", 5, reg.period, reg)
                tp = ledger.issue_ticket("station-a", "pallets", 5, reg.period, reg)
                journal = StationJournal(d / "s.db", "station-a", reg.period)
                journal.pull_tickets([th, tp])
                journal.offline_promise(2, 3, request_id="r1")
                journal.drain_for_sync(ledger)
                ledger.close_ticket(tp.ticket_id)  # 退托位未用 2
                report = {r["pool_id"]: r for r in ledger.weekly_reconciliation(reg.period)}
                h = report[th.pool_id]
                p = report[tp.pool_id]
                self.assertEqual((h["settled"], h["allocated_unused"], h["reallocatable"]),
                                 (2, 3, 0))  # 工时单仍 open：未用 3 计入已划未用
                self.assertEqual((p["settled"], p["allocated_unused"], p["reallocatable"]),
                                 (3, 0, 2))  # 托位单已关：未用 2 退回可再分配
                for r in report.values():
                    self.assertTrue(r["identity_invariant_holds"])
                journal.close()
            cat.close()
        finally:
            tmp.cleanup()


class DemandTest(unittest.TestCase):
    def test_batch_demand_from_real_rows_and_reopen(self):
        tmp = tempfile.TemporaryDirectory()
        d = Path(tmp.name)
        try:
            cat = Catalog(d / "cat.db")
            cat.publish("shared", SHARED_YAML)
            csv_path = d / "t.csv"
            csv_path.write_text(CSV, encoding="utf-8")
            repo = TaskRepository(d / "tasks.db", cat)
            repo.create_task("t1", "shared", "doc-0", csv_path)
            demand = demand_for_task(repo, cat, "t1")
            # web 两行各 2h/2 托，app 一行 1/1 => 5h, 5 托
            self.assertEqual((demand.hours, demand.pallets), (5, 5))
            self.assertEqual([r.matched_rule for r in demand.rows],
                             ["rule:web", "default", "rule:web"])
            # 重开：同任务同版本快照，结果一致
            repo.close()
            cat.close()
            cat2 = Catalog(d / "cat.db")
            repo2 = TaskRepository(d / "tasks.db", cat2)
            self.assertEqual(
                (demand_for_task(repo2, cat2, "t1").hours,
                 demand_for_task(repo2, cat2, "t1").pallets),
                (5, 5),
            )
            repo2.close()
            cat2.close()
        finally:
            tmp.cleanup()

    def test_unregistered_key_uses_default_and_missing_key_column_rejects(self):
        rule = ConversionRule(hours_per_row=1, pallets_per_row=1)
        # 未登记键值走 defaults 兜底
        demand = compute_batch_demand("t", [{"channel": "x"}], "channel", rule, {}, None)
        self.assertEqual((demand.hours, demand.pallets), (1, 1))
        self.assertEqual(demand.rows[0].matched_rule, "default")
        # 明细缺换算键列 -> 整批拒绝
        from confcat.errors import TaskRejected

        with self.assertRaises(TaskRejected):
            compute_batch_demand("t", [{"other": "x"}], "channel", rule, {}, None)


if __name__ == "__main__":
    unittest.main()
