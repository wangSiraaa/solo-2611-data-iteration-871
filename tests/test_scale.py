"""规模与深度合同测试：100 配置、万节点、10 万 CSV 行、深度 50、循环不无限展开。

默认使用受控规模以保持单测秒级完成；设置环境变量 CONFCAT_FULL=1 时跑满
题面规模（100 配置 / 10000 节点 / 100000 行）。
"""

from __future__ import annotations

import os
import tempfile
import time
import unittest
from pathlib import Path

from confcat.catalog import Catalog
from confcat.errors import CircularReferenceError, DepthLimitError
from confcat.ingest import TaskRepository
from confcat.materialize import materialize_all
from confcat.yaml_parser import parse_text

FULL = os.environ.get("CONFCAT_FULL") == "1"
N_CONFIGS = 100 if FULL else 12
N_NODES = 10_000 if FULL else 1_000
N_CSV_ROWS = 100_000 if FULL else 5_000
MAX_DEPTH = 50


def _build_deep_yaml(depth: int) -> str:
    """生成 depth 层嵌套映射（root 下 depth 个层级，最深一层挂 leaf）。"""
    lines = ["root:"]
    for d in range(depth):
        lines.append("  " * (d + 1) + f"lvl{d}:")
    lines.append("  " * (depth + 1) + "leaf: 1")
    return "\n".join(lines) + "\n"


class DepthAndCycleTest(unittest.TestCase):
    def test_deep_nesting_accepted_within_limit(self):
        # 48 层嵌套（含根块与 leaf 映射）落在 max_depth=50 保护内
        text = _build_deep_yaml(46)
        docs = parse_text(text, max_depth=MAX_DEPTH)
        mat = materialize_all(docs, max_depth=MAX_DEPTH)[0]
        node = mat.find(("root",) + tuple(f"lvl{i}" for i in range(46)) + ("leaf",))
        self.assertEqual(node.value, 1)

    def test_depth_exceeding_limit_rejected(self):
        text = _build_deep_yaml(MAX_DEPTH + 5)
        with self.assertRaises(DepthLimitError):
            materialize_all(parse_text(text, max_depth=MAX_DEPTH), max_depth=MAX_DEPTH)

    def test_cycle_does_not_expand_infinitely(self):
        cyc = "a: &a\n  self: *a\n"
        with self.assertRaises(CircularReferenceError):
            materialize_all(parse_text(cyc, max_depth=MAX_DEPTH), max_depth=MAX_DEPTH)


class ScaleTest(unittest.TestCase):
    def test_many_products_and_large_document(self):
        with tempfile.TemporaryDirectory() as td:
            cat = Catalog(Path(td) / "cat.db")
            # 单文档 ~N_NODES 个映射条目
            big = "doc:\n" + "".join(f"  key_{i}: value_{i}\n" for i in range(N_NODES))
            t0 = time.time()
            for c in range(N_CONFIGS):
                cat.publish(f"product-{c}", big)
            self.assertEqual(len(cat.list_products()), N_CONFIGS)
            q = cat.query(f"product-{N_CONFIGS - 1}", "doc-0", ("doc", f"key_{N_NODES - 1}"))
            self.assertEqual(q.value, f"value_{N_NODES - 1}")
            cat.close()
            elapsed = time.time() - t0
            if FULL:
                self.assertLess(elapsed, 60.0)

    def test_large_csv_pipeline(self):
        with tempfile.TemporaryDirectory() as td:
            csv_path = Path(td) / "big.csv"
            with csv_path.open("w", encoding="utf-8") as fh:
                fh.write("oid,region,chan\n")
                for i in range(N_CSV_ROWS):
                    region = "cn" if i % 2 else "us"
                    fh.write(f"{i},{region},web\n")
            cfg = (
                "ingest:\n  source_uri: file:///big.csv\n"
                "  column_mapping:\n    order_id: oid\n    region_name: region\n"
                "    channel: chan\n  partition_by:\n    - region_name\n"
            )
            cat = Catalog(Path(td) / "cat.db")
            cat.publish("p", cfg)
            repo = TaskRepository(Path(td) / "task.db", cat)
            t0 = time.time()
            summary = repo.create_task("big", "p", "doc-0", csv_path)
            self.assertEqual(summary.row_count, N_CSV_ROWS)
            rows = repo.get_rows("big")
            self.assertEqual(len(rows), N_CSV_ROWS)
            self.assertEqual(rows[0]["order_id"], "0")
            self.assertEqual(rows[-1]["order_id"], str(N_CSV_ROWS - 1))
            self.assertEqual(sum(len(v) for v in summary.partitions.values()), N_CSV_ROWS)
            repo.close()
            cat.close()
            if FULL:
                self.assertLess(time.time() - t0, 60.0)

    def test_wide_alias_reuse(self):
        # 一个锚点被大量别名复用：物化仍为独立子树，且来源链保留
        n_alias = 200 if FULL else 50
        text = "shared: &s\n  k: v\nuses:\n" + "".join(
            f"  u{i}: *s\n" for i in range(n_alias)
        )
        mat = materialize_all(parse_text(text))[0]
        uses = mat.find(("uses",))
        self.assertEqual(len(uses.entries), n_alias)
        for i in range(n_alias):
            node = mat.find(("uses", f"u{i}", "k"))
            self.assertEqual(node.value, "v")
            self.assertEqual([a.alias_name for a in node.prov.alias_chain], ["s"])


if __name__ == "__main__":
    unittest.main()
