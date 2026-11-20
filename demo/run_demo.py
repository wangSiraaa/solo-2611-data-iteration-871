"""端到端演示：目录发布 -> 人工检查 -> 接入任务 -> 修订有效性 -> 隔离 -> 重开回查。

运行：``python demo/run_demo.py``
产物：``data/demo_catalog.db``、``data/demo_tasks.db`` 以及控制台报告。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from confcat.catalog import Catalog
from confcat.codec import to_plain
from confcat.ingest import TaskRepository
from confcat.errors import TaskRejected

CATALOG_DB = ROOT / "data" / "demo_catalog.db"
TASK_DB = ROOT / "data" / "demo_tasks.db"
for f in (CATALOG_DB, TASK_DB):
    f.unlink(missing_ok=True)


def hr(title: str) -> None:
    print("\n" + "=" * 72)
    print(title)
    print("=" * 72)


def main() -> int:
    # 1. 发布多文档产品与单文档产品 ---------------------------------------- #
    hr("1) 发布配置产品（多文档同名锚点 + merge 合同专项）")
    with Catalog(CATALOG_DB) as cat:
        info_multi = cat.publish_file(
            "regional", ROOT / "samples" / "multidoc_config.yaml", doc_names=["doc-eu", "doc-us"]
        )
        info_orders = cat.publish_file("orders-product", ROOT / "samples" / "orders_config.yaml")
        info_merge = cat.publish_file("merge-lab", ROOT / "samples" / "merge_contract.yaml")
        print("regional v%s docs=%s" % (info_multi.version, info_multi.docs))
        print("orders-product v%s" % info_orders.version)
        print("merge-lab v%s" % info_merge.version)

        hr("2) 人工检查：merge 序列首键胜出 + 显式键恒覆盖 + 别名链")
        ingest = cat.export_plain("orders-product", "doc-0")["ingest"]
        print("ingest.tier   =", ingest["tier"], "  # priority_a 先提供，priority_b/defaults 只补缺")
        print("ingest.coupon =", ingest["coupon"], "  # 显式键覆盖 merge（出现在 << 之后仍胜出）")
        print("ingest.region =", ingest["region"], "  # 仅 defaults 提供，补缺")
        q = cat.query("orders-product", "doc-0", ("ingest", "tier"))
        print("tier 来源:", q.provenance.value_mark, "别名链:",
              [a.alias_name for a in q.provenance.alias_chain],
              "merge 证据:", [(e.source_index, e.won) for e in q.provenance.merge_evidence])

        hr("3) 人工检查：嵌套子映射不递归合并 + int/str 双键")
        lab = cat.export_plain("merge-lab", "doc-0")
        print("case_no_recursive.nested =", lab["case_no_recursive"]["nested"], "  # 整体占位，无 a/b")
        print("case_explicit_before.override_me =",
              lab["case_explicit_before"]["override_me"], "  # 显式键在 << 之前也胜出")
        print("quoted '<<' 普通键 =", lab["case_quoted_merge_marker"])
        typed = cat.export_plain("orders-product", "doc-0")["typed_keys"]
        print("typed_keys =", typed, "  # 1 与 '1' 并存")

        # 2. 接入任务 -------------------------------------------------------- #
        with TaskRepository(TASK_DB, cat) as repo:
            hr("4) 创建接入任务：读取本地 CSV 并按配置归一化")
            s1 = repo.create_task(
                "orders-eu-1", "regional", "doc-eu", ROOT / "data" / "orders_eu.csv"
            )
            print(s1)
            for row in repo.get_rows("orders-eu-1"):
                print(" ", row)
            print("分区:", repo.get_task("orders-eu-1").partitions)

            hr("5) 整次拒绝：列映射冲突 / 原列缺失 / 路径缺失 / 错误类型")
            bad_yaml = (
                "ingest:\n"
                "  source_uri: file:///x.csv\n"
                "  column_mapping:\n"
                "    a: oid\n"
                "    b: oid\n"
            )
            cat.publish("bad-conflict", bad_yaml)
            try:
                repo.create_task("bad-1", "bad-conflict", "doc-0", ROOT / "data" / "orders.csv")
            except TaskRejected as e:
                print("拒绝列冲突:", e)
            bad_missing_col = (
                "ingest:\n"
                "  source_uri: file:///x.csv\n"
                "  column_mapping:\n"
                "    a: ghost_col\n"
            )
            cat.publish("bad-missing-col", bad_missing_col)
            try:
                repo.create_task("bad-2", "bad-missing-col", "doc-0", ROOT / "data" / "orders.csv")
            except TaskRejected as e:
                print("拒绝原列缺失:", e)
            cat.publish("bad-path", "ingest:\n  source_uri: 123\n")
            try:
                repo.create_task("bad-3", "bad-path", "doc-0", ROOT / "data" / "orders.csv")
            except TaskRejected as e:
                print("拒绝错误类型:", e)
            # 必需路径缺失
            cat.publish("bad-missing-path", "other:\n  k: v\n")
            try:
                repo.create_task("bad-4", "bad-missing-path", "doc-0", ROOT / "data" / "orders.csv")
            except TaskRejected as e:
                print("拒绝路径缺失:", e)
            print("任务表中只有:", [t.task_id for t in repo.list_tasks()], "  # 被拒绝任务不留痕")

            # 3. 配置合并修订必须真实改变归一化结果 ---------------------------- #
            hr("6) 合并修订真实改变归一化结果（不是只改摘要）")
            v1 = (
                "base: &b\n  missing_value:\n    token: 'NA'\n    default: v1-default\n"
                "ingest:\n  <<: *b\n  source_uri: file:///orders.csv\n"
                "  column_mapping:\n    region_name: region\n    channel: chan\n"
            )
            cat.publish("revisable", v1)
            repo.create_task("rev-v1", "revisable", "doc-0", ROOT / "data" / "orders.csv")
            v2 = (
                "base: &b\n  missing_value:\n    token: 'NA'\n    default: v2-default\n"
                "ingest:\n  <<: *b\n  source_uri: file:///orders.csv\n"
                "  column_mapping:\n    region_name: region\n    channel: chan\n"
            )
            cat.publish("revisable", v2)
            repo.create_task("rev-v2", "revisable", "doc-0", ROOT / "data" / "orders.csv")
            rows_v1 = repo.get_rows("rev-v1")
            rows_v2 = repo.get_rows("rev-v2")
            print("v1 第 2 行:", rows_v1[1])
            print("v2 第 2 行:", rows_v2[1])
            assert rows_v1 != rows_v2 and rows_v1[1]["region_name"] != rows_v2[1]["region_name"]
            t1 = repo.get_task("rev-v1")
            t2 = repo.get_task("rev-v2")
            print("版本固定: rev-v1 -> v%s, rev-v2 -> v%s（在途任务不受再发布影响）"
                  % (t1.version, t2.version))
            assert t1.version == 1 and t2.version == 2

            # 4. 共享别名的消费隔离 ------------------------------------------- #
            hr("7) 共享别名：读取后消费方修改不影响其他任务")
            shared_yaml = (
                "common: &c\n"
                "  column_mapping:\n"
                "    order_id: oid\n"
                "    channel: chan\n"
                "ingest:\n"
                "  <<: *c\n"
                "  source_uri: file:///orders.csv\n"
            )
            cat.publish("shared", shared_yaml)
            repo.create_task("shared-a", "shared", "doc-0", ROOT / "data" / "orders.csv")
            repo.create_task("shared-b", "shared", "doc-0", ROOT / "data" / "orders.csv")
            rows_a = repo.get_rows("shared-a")
            rows_a[0]["order_id"] = "MUTATED"
            rows_b = repo.get_rows("shared-b")
            print("shared-a 内存篡改后 order_id =", rows_a[0]["order_id"])
            print("shared-b 持久化结果 order_id =", rows_b[0]["order_id"])
            assert rows_b[0]["order_id"] != "MUTATED"
            # 目录侧物化视图也不受影响
            live = cat.export_plain("shared", "doc-0")
            assert live["ingest"]["column_mapping"]["order_id"] == "oid"
            print("目录配置视图未被污染:", live["ingest"]["column_mapping"])

    # 5. 重开目录与任务仓，回查配置链 ---------------------------------------- #
    hr("8) 重开目录/任务仓：结果与配置来源链仍可回查")
    with Catalog(CATALOG_DB) as cat2, TaskRepository(TASK_DB, cat2) as repo2:
        print("完整性校验:", cat2.verify("orders-product"))
        q2 = cat2.query("orders-product", "doc-0", ("ingest", "coupon"))
        print("coupon =", q2.value, "来源行:", q2.provenance.value_mark.line,
              "显式覆盖=", q2.provenance.explicit,
              "被覆盖的 merge 候选数:",
              sum(1 for e in q2.provenance.merge_evidence if not e.won))
        # merge 首键胜出键的完整证据（含别名链与两个候选的胜负）
        q_tier = cat2.query("orders-product", "doc-0", ("ingest", "tier"))
        print("tier =", q_tier.value,
              "别名链:", [a.alias_name for a in q_tier.provenance.alias_chain],
              "证据:", [(e.source_index, e.won) for e in q_tier.provenance.merge_evidence])
        lineage = repo2.get_lineage("orders-eu-1")
        print("orders-eu-1 列血缘样例:", lineage[0]["target"], "<-",
              lineage[0]["source_column"], "来源行",
              lineage[0]["provenance"]["value_at"]["line"])
        print("orders-eu-1 行数:", repo2.get_task("orders-eu-1").row_count)
        print("分区 app 行:", repo2.get_partition_rows("orders-eu-1", "app"))

    hr("全部演示断言通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
