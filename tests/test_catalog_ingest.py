"""目录服务与消费仓集成测试：发布/版本/查询/逐行关系表/拒绝/隔离/重开。"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from confcat.catalog import Catalog
from confcat.codec import to_plain
from confcat.errors import CatalogError, TaskRejected
from confcat.ingest import TaskRepository

CONFIG = """
defaults: &defaults
  region: cn
prio: &prio
  tier: premium
ingest:
  <<:
    - *prio
    - *defaults
  source_uri: file:///data/orders.csv
  column_mapping:
    order_id: oid
    region_name: region
    channel: chan
  missing_value:
    token: "NA"
    default: unknown
  partition_by:
    - channel
"""

CSV = """oid,region,chan,extra
1,cn,web,x
2,NA,app,y
3,eu,web,z
"""


class CatalogTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / "cat.db"
        self.cat = Catalog(self.db)

    def tearDown(self):
        self.cat.close()
        self.tmp.cleanup()

    def test_publish_versioning_and_query(self):
        i1 = self.cat.publish("p", CONFIG)
        i2 = self.cat.publish("p", CONFIG.replace("premium", "gold"))
        self.assertEqual((i1.version, i2.version), (1, 2))
        self.assertEqual(self.cat.latest_version("p"), 2)
        # 旧版本仍可查
        q1 = self.cat.query("p", "doc-0", ("ingest", "tier"), version=1)
        q2 = self.cat.query("p", "doc-0", ("ingest", "tier"), version=2)
        self.assertEqual(q1.value, "premium")
        self.assertEqual(q2.value, "gold")
        self.assertTrue(q1.provenance.merge_evidence)

    def test_missing_path_and_typed_query(self):
        self.cat.publish("p", CONFIG)
        with self.assertRaises(CatalogError):
            self.cat.query("p", "doc-0", ("ingest", "nope",))
        q = self.cat.query_typed("p", "doc-0", ("ingest", "source_uri"), str)
        self.assertEqual(q.value, "file:///data/orders.csv")
        with self.assertRaises(CatalogError):
            self.cat.query_typed("p", "doc-0", ("ingest", "partition_by"), str)

    def test_unknown_product(self):
        with self.assertRaises(CatalogError):
            self.cat.latest_version("ghost")

    def test_verify_digest(self):
        self.cat.publish("p", CONFIG)
        self.assertEqual(self.cat.verify("p"), {"doc-0": "ok"})

    def test_reopen_preserves_provenance_and_key_types(self):
        self.cat.publish(
            "p",
            'k:\n  1: int-one\n  "1": str-one\n',
        )
        self.cat.close()
        cat2 = Catalog(self.db)
        doc = cat2.load_document("p", "doc-0")
        k = doc.root.entries["k"]
        self.assertEqual(k.entries[1].value, "int-one")
        self.assertEqual(k.entries["1"].value, "str-one")
        cat2.close()


class IngestTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        d = Path(self.tmp.name)
        self.cat_db = d / "cat.db"
        self.task_db = d / "task.db"
        self.csv_path = d / "orders.csv"
        self.csv_path.write_text(CSV, encoding="utf-8")
        self.cat = Catalog(self.cat_db)
        self.cat.publish("p", CONFIG)
        self.repo = TaskRepository(self.task_db, self.cat)

    def tearDown(self):
        self.repo.close()
        self.cat.close()
        self.tmp.cleanup()

    def test_normalized_rows_truth(self):
        summary = self.repo.create_task("t1", "p", "doc-0", self.csv_path)
        self.assertEqual(summary.row_count, 3)
        rows = self.repo.get_rows("t1")
        # 逐行手写真值：列重命名 + NA 缺失值兜底 + 列序按映射目标
        self.assertEqual(
            rows,
            [
                {"order_id": "1", "region_name": "cn", "channel": "web"},
                {"order_id": "2", "region_name": "unknown", "channel": "app"},
                {"order_id": "3", "region_name": "eu", "channel": "web"},
            ],
        )
        self.assertEqual(list(rows[0].keys()), ["order_id", "region_name", "channel"])
        self.assertEqual(summary.partitions, {"web": [0, 2], "app": [1]})
        self.assertEqual(
            self.repo.get_partition_rows("t1", "app"),
            [{"order_id": "2", "region_name": "unknown", "channel": "app"}],
        )

    def test_column_lineage_keeps_source_and_provenance(self):
        self.repo.create_task("t1", "p", "doc-0", self.csv_path)
        lineage = self.repo.get_lineage("t1")
        self.assertEqual([c["target"] for c in lineage], ["order_id", "region_name", "channel"])
        self.assertEqual([c["source_column"] for c in lineage], ["oid", "region", "chan"])
        for c in lineage:
            self.assertEqual(c["header_line"], 1)
            self.assertEqual(c["provenance"]["path"][:2], ["ingest", "column_mapping"])

    def test_reject_column_conflict(self):
        bad = (
            "ingest:\n  source_uri: file:///x.csv\n"
            "  column_mapping:\n    a: oid\n    b: oid\n"
        )
        self.cat.publish("bad", bad)
        with self.assertRaises(TaskRejected) as ctx:
            self.repo.create_task("x", "bad", "doc-0", self.csv_path)
        self.assertEqual(ctx.exception.reason, "column-mapping-conflict")
        self.assertNotIn("x", [t.task_id for t in self.repo.list_tasks()])

    def test_reject_missing_source_column(self):
        bad = (
            "ingest:\n  source_uri: file:///x.csv\n"
            "  column_mapping:\n    a: ghost\n"
        )
        self.cat.publish("bad", bad)
        with self.assertRaises(TaskRejected) as ctx:
            self.repo.create_task("x", "bad", "doc-0", self.csv_path)
        self.assertEqual(ctx.exception.reason, "missing-source-column")

    def test_reject_bad_type_and_missing_path(self):
        self.cat.publish("badtype", "ingest:\n  source_uri: 123\n  column_mapping:\n    a: oid\n")
        with self.assertRaises(TaskRejected):
            self.repo.create_task("x", "badtype", "doc-0", self.csv_path)
        self.cat.publish("nopath", "other:\n  k: 1\n")
        with self.assertRaises(TaskRejected) as ctx:
            self.repo.create_task("y", "nopath", "doc-0", self.csv_path)
        self.assertEqual(ctx.exception.reason, "missing-path")

    def test_reject_partition_key_missing(self):
        cfg = (
            "ingest:\n  source_uri: file:///x.csv\n"
            "  column_mapping:\n    oid: oid\n    chan: chan\n"
            "  partition_by:\n    - chan\n"
        )
        # 没有 default：NA 分区键保持 NA token -> 不视为缺失；构造真正空值
        self.csv_path.write_text("oid,chan\n1,web\n2,\n", encoding="utf-8")
        self.cat.publish("p2", cfg)
        with self.assertRaises(TaskRejected) as ctx:
            self.repo.create_task("z", "p2", "doc-0", self.csv_path)
        self.assertEqual(ctx.exception.reason, "partition-key-missing")

    def test_republished_product_does_not_affect_inflight_task(self):
        self.repo.create_task("t1", "p", "doc-0", self.csv_path)
        before = self.repo.get_task("t1")
        # 发布新版本与另一个无关产品
        self.cat.publish("p", CONFIG.replace("unknown", "changed-default"))
        self.cat.publish("other", CONFIG)
        after = self.repo.get_task("t1")
        self.assertEqual(before.version, after.version)
        self.assertEqual(
            self.repo.get_rows("t1")[1]["region_name"], "unknown"
        )  # 仍是旧版本归一化结果

    def test_revision_changes_normalized_output(self):
        v1 = (
            "base: &b\n  missing_value:\n    token: 'NA'\n    default: d1\n"
            "ingest:\n  <<: *b\n  source_uri: file:///x.csv\n"
            "  column_mapping:\n    region_name: region\n"
        )
        v2 = v1.replace("default: d1", "default: d2")
        self.cat.publish("r", v1)
        self.cat.publish("r", v2)
        self.repo.create_task("r1", "r", "doc-0", self.csv_path, version=1)
        self.repo.create_task("r2", "r", "doc-0", self.csv_path, version=2)
        self.assertEqual(self.repo.get_rows("r1")[1]["region_name"], "d1")
        self.assertEqual(self.repo.get_rows("r2")[1]["region_name"], "d2")

    def test_shared_alias_consumer_isolation(self):
        shared = (
            "common: &c\n  column_mapping:\n    order_id: oid\n    channel: chan\n"
            "ingest:\n  <<: *c\n  source_uri: file:///x.csv\n"
        )
        self.cat.publish("s", shared)
        self.repo.create_task("sa", "s", "doc-0", self.csv_path)
        self.repo.create_task("sb", "s", "doc-0", self.csv_path)
        rows_a = self.repo.get_rows("sa")
        rows_a[0]["order_id"] = "MUTATED"
        rows_b = self.repo.get_rows("sb")
        self.assertNotEqual(rows_b[0]["order_id"], "MUTATED")
        # 目录视图也未被污染
        self.assertEqual(
            self.cat.export_plain("s", "doc-0")["ingest"]["column_mapping"]["order_id"],
            "oid",
        )

    def test_reopen_repository_reviews_chain(self):
        self.repo.create_task("t1", "p", "doc-0", self.csv_path)
        params = self.repo.get_params("t1")
        sources = self.repo.get_param_sources("t1")
        self.repo.close()
        self.cat.close()
        cat2 = Catalog(self.cat_db)
        repo2 = TaskRepository(self.task_db, cat2)
        self.assertEqual(repo2.get_rows("t1")[0], {"order_id": "1", "region_name": "cn", "channel": "web"})
        self.assertEqual(repo2.get_params("t1"), params)
        self.assertTrue(repo2.get_param_sources("t1")[0]["provenance"])
        self.assertEqual(cat2.verify("p"), {"doc-0": "ok"})
        repo2.close()
        cat2.close()


if __name__ == "__main__":
    unittest.main()
