"""端到端演示：额度池身份目录 -> 中心划单 -> 两站断网离线承诺 -> 重联幂等结算
-> 关单/过期拒收回执 -> 重命名身份不变 -> 周结三笔量 -> 重开复核。

运行：``python demo/run_quota_demo.py``
产物：``data/demo_quota_catalog.db``、``data/demo_quota_ledger.db``、
      ``data/demo_station_a.db``、``data/demo_station_b.db``。
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from confcat.catalog import Catalog
from confcat.demand import demand_for_task
from confcat.errors import OfflineRejected, QuotaConfigError, QuotaOverAllocated, TicketError
from confcat.ingest import TaskRepository
from confcat.quota import apply_rename, registry_from_catalog
from confcat.quota_ledger import QuotaLedger, StationJournal

CATALOG_DB = ROOT / "data" / "demo_quota_catalog.db"
LEDGER_DB = ROOT / "data" / "demo_quota_ledger.db"
TASK_DB = ROOT / "data" / "demo_quota_tasks.db"
STATION_A_DB = ROOT / "data" / "demo_station_a.db"
STATION_B_DB = ROOT / "data" / "demo_station_b.db"
STATION_EXPIRED_DB = ROOT / "data" / "demo_station_expired.db"
for f in (CATALOG_DB, LEDGER_DB, TASK_DB, STATION_A_DB, STATION_B_DB, STATION_EXPIRED_DB):
    f.unlink(missing_ok=True)

PERIOD = "2026-W47"


def hr(title: str) -> None:
    print("\n" + "=" * 76)
    print(title)
    print("=" * 76)


def show_registry(reg) -> None:
    print(f"周期 {reg.period} 共 {len(reg.pools)} 个池（身份稳定，同名同值也不合并）：")
    for pid, pool in sorted(reg.pools.items()):
        via = pool.views[0].via_anchor or "显式独立池"
        print(f"  {pid:30s} {pool.name:26s} 限额={pool.limit:<2} 读法分站={pool.stations()} 身份依据={via}")


def main() -> int:
    # 1. 发布额度池产品并构建带稳定身份的池目录 ------------------------------ #
    hr("1) 发布 YAML 产品 -> 带稳定身份与结算周期的额度池目录")
    with Catalog(CATALOG_DB) as cat:
        info = cat.publish_file("quota-product", ROOT / "samples" / "quota_config.yaml")
        reg = registry_from_catalog(cat, "quota-product", "doc-0", info.version)
        show_registry(reg)
        shared = reg.pool_for("station-a", "hours")
        assert reg.pool_for("station-b", "hours").pool_id == shared.pool_id, "别名两读法必须同池"
        pa = reg.pool_for("station-a", "pallets")
        pb = reg.pool_for("station-b", "pallets")
        assert pa.pool_id != pb.pool_id and pa.limit == pb.limit == 4, "同值显式/锚点池不得合并"
        print("断言：station-a/b 工时同池（共 5）；两站托位同限额 4 却是两个池")

        # 2. 消费仓：真实 CSV 任务明细按换算约定算批次需求 -------------------- #
        hr("2) 消费仓按已登记换算约定，从真实 CSV 明细计算一批任务的两种资源需求")
        task_db = TASK_DB
        task_db.unlink(missing_ok=True)
        with TaskRepository(task_db, cat) as repo:
            repo.create_task("batch-1", "quota-product", "doc-0", ROOT / "data" / "quota_tasks.csv")
            demand = demand_for_task(repo, cat, "batch-1")
            print(f"任务 batch-1 共 {demand.row_count} 行 => 同时需要 {demand.hours} 工时、"
                  f"{demand.pallets} 托位（divisor={demand.pallets_divisor}）")
            for row in demand.rows:
                print("  行", row.row_no, row.key_value, "->", row.hours, "工时",
                      row.pallets, "托位 [", row.matched_rule, "]")
            assert (demand.hours, demand.pallets) == (10, 4)

        # 3. 中心划单：共享池别名两站不能各领 3；独立池可以 ------------------- #
        hr("3) 中心独占划单：共享池（别名同池）vs 同值独立池")
        with QuotaLedger(LEDGER_DB) as ledger:
            ledger.register_period(reg)
            # 3a. 共享工时池只有 5：A 领 3 后，B 再领 3 必超额
            t_a_h = ledger.issue_ticket("station-a", "hours", 3, PERIOD, reg)
            print("A 站工时划单 3：", t_a_h.ticket_id, "池=", t_a_h.pool_id)
            try:
                ledger.issue_ticket("station-b", "hours", 3, PERIOD, reg)
            except QuotaOverAllocated as e:
                print("B 站再领 3 被中心拒绝：", e)
            # B 改领 2：3+2 == 5，恰好占满共享池
            t_b_h = ledger.issue_ticket("station-b", "hours", 2, PERIOD, reg)
            print("B 站工时改领 2 成功：", t_b_h.ticket_id)
            # 3b. station-c 是各 5 的独立池：可以各领 3，与共享池互不影响
            t_c_h = ledger.issue_ticket("station-c", "hours", 3, PERIOD, reg)
            t_c_p = ledger.issue_ticket("station-c", "pallets", 3, PERIOD, reg)
            print("C 站独立池各领 3：", t_c_h.pool_id, "/", t_c_p.pool_id)

            # 题面原子例：工时单 3 小时；托位单先只给 1
            t_a_p_short = ledger.issue_ticket("station-a", "pallets", 1, PERIOD, reg)
            print("A 站托位先划 1：", t_a_p_short.ticket_id, "池=", t_a_p_short.pool_id)
            # B 站正常单
            t_b_p = ledger.issue_ticket("station-b", "pallets", 3, PERIOD, reg)
            print("B 站托位划单 3：", t_b_p.ticket_id)

            # 4. 两站断网：本地额度单内同时承诺两种资源，原子成败 -------------- #
            hr("4) 两站短时离线：只在本单内同时承诺工时+托位，任一不足两边都不扣")
            with StationJournal(STATION_A_DB, "station-a", PERIOD) as ja, \
                 StationJournal(STATION_B_DB, "station-b", PERIOD) as jb:
                ja.pull_tickets(ledger.open_tickets("station-a", PERIOD))
                jb.pull_tickets(ledger.open_tickets("station-b", PERIOD))
                print("A 站本地单：", [(t["resource"], t["amount"]) for t in ja.local_tickets()])
                print("B 站本地单：", [(t["resource"], t["amount"]) for t in jb.local_tickets()])

                # 4a. 题面例：需 2 工时 2 托位；托位单仅剩 1 => 整单不扣
                try:
                    ja.offline_promise(2, 2, task_id="batch-1", request_id="req-A-1")
                except OfflineRejected as e:
                    print("A 站承诺 2h/2托 被拒收：", e)
                tickets_after = {t["resource"]: (t["amount"], t["used"]) for t in ja.local_tickets()}
                print("拒收后本地单（amount, used）：", tickets_after, " # 两种资源均未扣")
                assert all(used == 0 for _amt, used in tickets_after.values())
                rcp = ja.local_receipts()[0]
                print("可复核回执：", rcp["receipt_id"], rcp["reason"], rcp["context"])

                # 4b. 中心补足托位（再划 2，独立池余 3），重试一次仅扣 2h/2托
                t_a_p_more = ledger.issue_ticket("station-a", "pallets", 2, PERIOD, reg)
                ja.pull_tickets([t_a_p_more])
                # 注：离线现场只在 open 单内承诺；补的第二张托位单先到先得
                ok = ja.offline_promise(2, 2, task_id="batch-1", request_id="req-A-2")
                print("补足后重试成功：", ok.request_id, "扣", ok.hours_need, "工时",
                      ok.pallets_need, "托位")
                left = {t["resource"]: t["amount"] - t["used"] for t in ja.local_tickets()}
                print("A 站承诺后各单余额：", left)

                # 4c. B 站离线成功一条 + 一条超额被拒
                ok_b = jb.offline_promise(2, 2, task_id="batch-1", request_id="req-B-1")
                print("B 站承诺成功：", ok_b.request_id)
                try:
                    jb.offline_promise(2, 3, task_id="batch-1", request_id="req-B-2")
                except OfflineRejected as e:
                    print("B 站超额承诺被拒收：", e.reason)

                # 5. 重联：交叉回传，幂等结算，每个承诺只结算一次 -------------- #
                hr("5) 重联交叉回传：幂等结算（重复 request_id 不双扣）")
                res_a = ja.drain_for_sync(ledger)
                res_b = jb.drain_for_sync(ledger)
                print("A 站回传结算：", [(r.request_id, r.status, "dup=" + str(r.duplicated)) for r in res_a])
                print("B 站回传结算：", [(r.request_id, r.status, "dup=" + str(r.duplicated)) for r in res_b])

                # 重复回传同一承诺：返回原结果，不二次扣减
                again = ledger.settle_promise({
                    "request_id": "req-A-2", "station": "station-a", "period": PERIOD,
                    "hours_ticket": ok.hours_ticket, "pallets_ticket": ok.pallets_ticket,
                    "hours_need": 2, "pallets_need": 2, "task_id": "batch-1",
                })
                print("req-A-2 重复回传：", again.status, "duplicated =", again.duplicated)
                assert again.duplicated and again.hours_need == 2
                h_row = ledger.get_ticket(ok.hours_ticket)
                print("工时单 used 仍为", h_row.used, "（未被重复回传双扣）")
                assert h_row.used == 2

                # 6. 关单退回未用；关闭/过期单的新离线承诺拒收并给回执 ---------- #
                hr("6) 退回未用额度后，关闭单/过期单上的新承诺一律拒收（可复核回执）")
                before = len(ledger.list_receipts())
                closed = ledger.close_ticket(t_b_p.ticket_id)
                print("关闭 B 站托位单", closed.ticket_id, "未用", closed.amount - closed.used,
                      "退回中心可再分配；状态 =", closed.status)
                # 中心侧对关闭单结算：金额本身足够，但单已关闭 -> TicketError + 中心回执
                bad = {
                    "request_id": "req-B-stale", "station": "station-b", "period": PERIOD,
                    "hours_ticket": t_b_h.ticket_id, "pallets_ticket": t_b_p.ticket_id,
                    "hours_need": 0, "pallets_need": 1, "task_id": "batch-1",
                }
                ledger.sync_pending("station-b", [bad])
                center_rcp = ledger.list_receipts()[-1]
                print("关闭单新承诺中心回执：", center_rcp["receipt_id"], center_rcp["reason"],
                      "|", center_rcp["detail"])
                assert len(ledger.list_receipts()) == before + 1

                # 过期单：上一周期的单在当前周期离线承诺被拒（两类单配齐，
                # 真正命中周期校验而不是缺单）
                old_reg = _build_old_period_registry(cat)
                ledger.register_period(old_reg)
                t_exp_h = ledger.issue_ticket("station-a", "hours", 2, "2026-W46", old_reg)
                t_exp_p = ledger.issue_ticket("station-a", "pallets", 2, "2026-W46", old_reg)
                expired_journal = StationJournal(
                    STATION_EXPIRED_DB, "station-a", PERIOD
                )
                expired_journal.pull_tickets([t_exp_h, t_exp_p])
                try:
                    expired_journal.offline_promise(1, 1, request_id="req-expired")
                except OfflineRejected as e:
                    print("过期周期额度单离线承诺拒收：", e.reason, e.context)
                    assert e.reason == "ticket-expired"
                exp_rcp = expired_journal.local_receipts()[-1]
                print("过期单回执：", exp_rcp["receipt_id"], exp_rcp["reason"])
                expired_journal.close()
                expired = ledger.expire_period("2026-W46")
                print("W46 周期过期处理：", [(t.ticket_id, t.status) for t in expired])
                assert all(t.status == "expired" for t in expired)

            # 7. 池重命名身份不变：旧发放仍占原余额 ----------------------------- #
            hr("7) 新版本池重命名但 pool_id 不变：旧额度单/承诺仍占原余额")
            renamed_text = (ROOT / "samples" / "quota_config.yaml").read_text(encoding="utf-8").replace(
                "name: shared-sorting-hours", "name: shared-sorting-hours-renamed"
            )
            v2 = cat.publish("quota-product", renamed_text, source_path="quota_config.yaml#renamed")
            reg_v2 = apply_rename(reg, registry_from_catalog(cat, "quota-product", "doc-0", v2.version))
            pool_v2 = reg_v2.pool_for("station-a", "hours")
            print("工时池 pool_id =", pool_v2.pool_id, "新名 =", pool_v2.name,
                  "旧名 =", pool_v2.renamed_from)
            assert pool_v2.pool_id == shared.pool_id and pool_v2.renamed_from == "shared-sorting-hours"
            old_ticket = ledger.get_ticket(t_a_h.ticket_id)
            print("旧额度单仍绑定原 pool_id：", old_ticket.pool_id, "used =", old_ticket.used,
                  "（重命名不迁移、不重置余额）")
            assert old_ticket.pool_id == shared.pool_id

            # 8. 周结：按池复核三笔量 ------------------------------------------- #
            hr("8) 中心周结：按池复核 已结算 / 已划未用 / 可再分配 三笔量")
            report = ledger.weekly_reconciliation(PERIOD)
            for line in report:
                print(
                    f"  {line['pool_id']:30s} 限额={line['limit']:<2}"
                    f" 已结算={line['settled']:<2} 已划未用={line['allocated_unused']:<2}"
                    f" 可再分配={line['reallocatable']:<2} 恒等式={line['identity_invariant_holds']}"
                )
            assert all(line["identity_invariant_holds"] for line in report)
            total_check = sum(
                line["settled"] + line["allocated_unused"] + line["reallocatable"]
                for line in report
            ) == sum(line["limit"] for line in report)
            assert total_check
            print("全部池满足：限额 == 已结算 + 已划未用 + 可再分配")

    # 9. 重开全部库：原任务照原配置重开，账本/回执/周结可回查 ------------------ #
    hr("9) 重开目录/账本/分站库：原任务照原配置重开，历史链可复核")
    with Catalog(CATALOG_DB) as cat2, \
         QuotaLedger(LEDGER_DB) as ledger2, \
         TaskRepository(TASK_DB, cat2) as repo2, \
         StationJournal(STATION_A_DB, "station-a", PERIOD) as ja3:
        demand2 = demand_for_task(repo2, cat2, "batch-1")
        print("原任务 batch-1 重开重算需求：", demand2.hours, "工时", demand2.pallets, "托位",
              "（与首跑一致 =", (demand2.hours, demand2.pallets) == (10, 4), "）")
        print("已结算承诺数：", len(ledger2.list_promises()),
              "| 中心回执数：", len(ledger2.list_receipts()),
              "| A 站本地回执数：", len(ja3.local_receipts()))
        reopen_report = ledger2.weekly_reconciliation(PERIOD)
        print("重开后周结可复算，池数：", len(reopen_report))
        assert reopen_report[0]["identity_invariant_holds"]

    hr("全部额度池/离线额度单演示断言通过")
    return 0


def _build_old_period_registry(cat: Catalog):
    """构造上一周期（W46）的池目录快照，用于过期单演示。"""
    text = (ROOT / "samples" / "quota_config.yaml").read_text(encoding="utf-8").replace(
        'period: "2026-W47"', 'period: "2026-W46"'
    )
    info = cat.publish("quota-product-w46", text)
    return registry_from_catalog(cat, "quota-product-w46", "doc-0", info.version)


if __name__ == "__main__":
    raise SystemExit(main())
