"""消费仓：数据集接入任务管理与结果持久化。

任务合同
========
* 每任务绑定 **产品 + 版本快照 + 文档 + 一组明确配置路径**；
* 类型化取得 ``source_uri``（str）、``column_mapping``（str->str 映射）、
  缺失值规则（可选映射）与分区键（可选序列）；
* 路径缺失、错误类型、列映射冲突 -> 拒绝整次任务，任务与结果均不落库；
* 归一化结果、每列血缘（配置来源 + 原 CSV 列）、分区信息全部写入 SQLite；
* 重开仓库后任务结果仍可逐行回查，并可回查到当时的配置来源链；
* 配置快照按版本深拷贝持有，后续发布新版本不影响在途/已完成任务。
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from .catalog import Catalog
from .codec import decode_document
from .csv_pipeline import MissingRule, NormalizedTable, normalize
from .errors import CatalogError, TaskRejected
from .model import MatMapping, MatScalar, MatSequence, MaterializedDocument, Provenance

_SCHEMA = """
CREATE TABLE IF NOT EXISTS task (
    task_id       TEXT PRIMARY KEY,
    product       TEXT NOT NULL,
    version       INTEGER NOT NULL,
    doc_name      TEXT NOT NULL,
    created_at    TEXT NOT NULL,
    source_csv    TEXT NOT NULL,
    status        TEXT NOT NULL,
    paths_json    TEXT NOT NULL,
    params_json   TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS task_column (
    task_id        TEXT NOT NULL,
    target_column  TEXT NOT NULL,
    source_column  TEXT NOT NULL,
    ordinal        INTEGER NOT NULL,
    header_line    INTEGER NOT NULL,
    provenance     TEXT,
    PRIMARY KEY (task_id, target_column),
    FOREIGN KEY (task_id) REFERENCES task(task_id)
);
CREATE TABLE IF NOT EXISTS task_row (
    task_id    TEXT NOT NULL,
    row_no     INTEGER NOT NULL,
    values_json TEXT NOT NULL,
    PRIMARY KEY (task_id, row_no),
    FOREIGN KEY (task_id) REFERENCES task(task_id)
);
CREATE TABLE IF NOT EXISTS task_partition (
    task_id    TEXT NOT NULL,
    key_value  TEXT NOT NULL,
    row_nos    TEXT NOT NULL,
    PRIMARY KEY (task_id, key_value),
    FOREIGN KEY (task_id) REFERENCES task(task_id)
);
CREATE TABLE IF NOT EXISTS task_param_source (
    task_id   TEXT NOT NULL,
    param     TEXT NOT NULL,
    path      TEXT NOT NULL,
    prov_json TEXT,
    PRIMARY KEY (task_id, param),
    FOREIGN KEY (task_id) REFERENCES task(task_id)
);
"""

# 任务读取的固定配置路径（题面要求“一组明确路径”）
PATH_SOURCE_URI = ("ingest", "source_uri")
PATH_COLUMN_MAPPING = ("ingest", "column_mapping")
PATH_MISSING = ("ingest", "missing_value")
PATH_PARTITION = ("ingest", "partition_by")


@dataclass
class TaskSummary:
    task_id: str
    product: str
    version: int
    doc_name: str
    status: str
    source_csv: str
    row_count: int
    columns: tuple[str, ...]
    partitions: dict[str, list[int]]
    created_at: str


class TaskRepository:
    def __init__(self, db_path: str | Path, catalog: Catalog):
        self.db_path = str(db_path)
        self.catalog = catalog
        self.conn = sqlite3.connect(self.db_path)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(_SCHEMA)
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> "TaskRepository":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # ------------------------------------------------------------------ #
    def _doc_snapshot(self, product: str, doc_name: str, version: int) -> MaterializedDocument:
        # 从目录行读取 JSON 再解码：天然是独立深拷贝，与目录缓存互不共享对象
        version = self.catalog._resolve_version(product, version)
        row = self.catalog.conn.execute(
            "SELECT snapshot FROM document WHERE product = ? AND version = ? AND doc_name = ?",
            (product, version, doc_name),
        ).fetchone()
        if row is None:
            raise CatalogError(f"文档 {doc_name!r} 不存在（产品 {product!r} v{version}）")
        return decode_document(json.loads(row["snapshot"]))

    def _typed_get(self, doc: MaterializedDocument, path: tuple, expected, param: str):
        try:
            node = doc.find(path)
        except (KeyError, IndexError):
            raise TaskRejected(f"必需配置路径缺失: /{'/'.join(map(str, path))}", path=path, reason="missing-path")
        value = node
        if expected is str:
            if not isinstance(node, MatScalar) or not isinstance(node.value, str):
                raise TaskRejected(f"参数 {param} 需要字符串", path=path, reason="bad-type")
        elif expected is int:
            if not isinstance(node, MatScalar) or isinstance(node.value, bool) or not isinstance(node.value, int):
                raise TaskRejected(f"参数 {param} 需要整数", path=path, reason="bad-type")
        elif expected == "mapping":
            if not isinstance(node, MatMapping):
                raise TaskRejected(f"参数 {param} 需要映射", path=path, reason="bad-type")
        elif expected == "sequence":
            if not isinstance(node, MatSequence):
                raise TaskRejected(f"参数 {param} 需要序列", path=path, reason="bad-type")
        return node

    def create_task(
        self,
        task_id: str,
        product: str,
        doc_name: str,
        csv_path: str | Path,
        version: int | None = None,
    ) -> TaskSummary:
        """创建并执行接入任务。任何校验失败 -> 整次拒绝，不写任何任务数据。"""
        if self.conn.execute("SELECT 1 FROM task WHERE task_id = ?", (task_id,)).fetchone():
            raise TaskRejected(f"任务 {task_id!r} 已存在", reason="duplicate-task")

        version = self.catalog._resolve_version(product, version)
        doc = self._doc_snapshot(product, doc_name, version)

        # 1) 类型化取参（路径缺失/错误类型在这里整次拒绝）
        uri_node = self._typed_get(doc, PATH_SOURCE_URI, str, "source_uri")
        mapping_node = self._typed_get(doc, PATH_COLUMN_MAPPING, "mapping", "column_mapping")

        column_mapping: dict[str, str] = {}
        mapping_prov: dict[str, Provenance | None] = {}
        for key, child in mapping_node.entries.items():
            if not isinstance(key, str):
                raise TaskRejected(
                    "column_mapping 的键必须是字符串目标列名",
                    path=PATH_COLUMN_MAPPING + (key,),
                    reason="bad-type",
                )
            if not isinstance(child, MatScalar) or not isinstance(child.value, str):
                raise TaskRejected(
                    f"column_mapping[{key!r}] 必须是字符串原列名",
                    path=PATH_COLUMN_MAPPING + (key,),
                    reason="bad-type",
                )
            column_mapping[key] = child.value
            mapping_prov[key] = mapping_node.key_prov[key]
        if not column_mapping:
            raise TaskRejected("column_mapping 不能为空", path=PATH_COLUMN_MAPPING, reason="empty-mapping")

        missing_rule = MissingRule()
        try:
            miss_node = doc.find(PATH_MISSING)
            if not isinstance(miss_node, MatMapping):
                raise TaskRejected("missing_value 必须是映射", path=PATH_MISSING, reason="bad-type")
            token = ""
            default = None
            for key, child in miss_node.entries.items():
                if not isinstance(child, MatScalar) or not isinstance(child.value, str):
                    raise TaskRejected(
                        f"missing_value.{key} 必须是字符串", path=PATH_MISSING + (key,), reason="bad-type"
                    )
                if key == "token":
                    token = child.value
                elif key == "default":
                    default = child.value
                else:
                    raise TaskRejected(
                        f"missing_value 含未知键 {key!r}", path=PATH_MISSING + (key,), reason="unknown-key"
                    )
            missing_rule = MissingRule(token=token, default=default)
        except (KeyError, IndexError):
            pass  # 缺失值规则可选

        partition_by: tuple[str, ...] = ()
        try:
            part_node = doc.find(PATH_PARTITION)
            if not isinstance(part_node, MatSequence):
                raise TaskRejected("partition_by 必须是序列", path=PATH_PARTITION, reason="bad-type")
            parts: list[str] = []
            for idx, child in enumerate(part_node.items):
                if not isinstance(child, MatScalar) or not isinstance(child.value, str):
                    raise TaskRejected(
                        f"partition_by[{idx}] 必须是字符串", path=PATH_PARTITION + (idx,), reason="bad-type"
                    )
                parts.append(child.value)
            partition_by = tuple(parts)
        except (KeyError, IndexError):
            pass

        # 2) 实际读取本地 CSV 并归一化（列冲突/分区缺失等在这里整次拒绝）
        table: NormalizedTable = normalize(
            csv_path=csv_path,
            column_mapping=column_mapping,
            mapping_prov=mapping_prov,
            missing=missing_rule,
            partition_by=partition_by,
        )
        # 合同要求读取合成本地 CSV：source_uri 是配置声明，实际文件以传入路径为准；
        # 两者记录在案但不强制字符串相等（本地合成场景允许不同落盘位置）。
        source_uri = uri_node.value

        # 3) 全部成功后才落库（单事务，异常回滚 => 不留半截任务）
        created_at = datetime.now(timezone.utc).isoformat()
        paths = {
            "source_uri": list(PATH_SOURCE_URI),
            "column_mapping": list(PATH_COLUMN_MAPPING),
            "missing_value": list(PATH_MISSING),
            "partition_by": list(PATH_PARTITION),
        }
        params = {
            "source_uri": source_uri,
            "column_mapping": column_mapping,
            "missing_value": {"token": missing_rule.token, "default": missing_rule.default},
            "partition_by": list(partition_by),
        }
        try:
            self.conn.execute(
                "INSERT INTO task(task_id, product, version, doc_name, created_at,"
                " source_csv, status, paths_json, params_json) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    task_id,
                    product,
                    version,
                    doc_name,
                    created_at,
                    str(csv_path),
                    "succeeded",
                    json.dumps(paths, ensure_ascii=False),
                    json.dumps(params, ensure_ascii=False),
                ),
            )
            for ordinal, col in enumerate(table.lineage):
                self.conn.execute(
                    "INSERT INTO task_column(task_id, target_column, source_column, ordinal,"
                    " header_line, provenance) VALUES(?, ?, ?, ?, ?, ?)",
                    (
                        task_id,
                        col.target,
                        col.source_column,
                        ordinal,
                        col.header_line,
                        json.dumps(col.provenance.as_dict(), ensure_ascii=False)
                        if col.provenance
                        else None,
                    ),
                )
            self.conn.executemany(
                "INSERT INTO task_row(task_id, row_no, values_json) VALUES(?, ?, ?)",
                [
                    (task_id, i, json.dumps(row, ensure_ascii=False))
                    for i, row in enumerate(table.rows)
                ],
            )
            for key_value, row_nos in table.partitions.items():
                self.conn.execute(
                    "INSERT INTO task_partition(task_id, key_value, row_nos) VALUES(?, ?, ?)",
                    (task_id, key_value, json.dumps(row_nos)),
                )
            for name, node in (
                ("source_uri", uri_node),
                ("column_mapping", mapping_node),
            ):
                self.conn.execute(
                    "INSERT INTO task_param_source(task_id, param, path, prov_json) VALUES(?, ?, ?, ?)",
                    (
                        task_id,
                        name,
                        "/" + "/".join(map(str, paths[name])),
                        json.dumps(node.prov.as_dict(), ensure_ascii=False) if node.prov else None,
                    ),
                )
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise

        return TaskSummary(
            task_id=task_id,
            product=product,
            version=version,
            doc_name=doc_name,
            status="succeeded",
            source_csv=str(csv_path),
            row_count=len(table.rows),
            columns=table.columns,
            partitions=table.partitions,
            created_at=created_at,
        )

    # ------------------------------------------------------------------ #
    def list_tasks(self) -> list[TaskSummary]:
        rows = self.conn.execute("SELECT * FROM task ORDER BY created_at").fetchall()
        return [self._summary(r) for r in rows]

    def get_task(self, task_id: str) -> TaskSummary:
        row = self.conn.execute("SELECT * FROM task WHERE task_id = ?", (task_id,)).fetchone()
        if row is None:
            raise CatalogError(f"任务 {task_id!r} 不存在")
        return self._summary(row)

    def _summary(self, row: sqlite3.Row) -> TaskSummary:
        part_rows = self.conn.execute(
            "SELECT key_value, row_nos FROM task_partition WHERE task_id = ? ORDER BY key_value",
            (row["task_id"],),
        ).fetchall()
        partitions = {r["key_value"]: json.loads(r["row_nos"]) for r in part_rows}
        col_rows = self.conn.execute(
            "SELECT target_column FROM task_column WHERE task_id = ? ORDER BY ordinal",
            (row["task_id"],),
        ).fetchall()
        count = self.conn.execute(
            "SELECT COUNT(*) AS c FROM task_row WHERE task_id = ?", (row["task_id"],)
        ).fetchone()["c"]
        return TaskSummary(
            task_id=row["task_id"],
            product=row["product"],
            version=row["version"],
            doc_name=row["doc_name"],
            status=row["status"],
            source_csv=row["source_csv"],
            row_count=count,
            columns=tuple(r["target_column"] for r in col_rows),
            partitions=partitions,
            created_at=row["created_at"],
        )

    def get_rows(self, task_id: str) -> list[dict[str, str]]:
        self.get_task(task_id)  # 存在性检查
        rows = self.conn.execute(
            "SELECT values_json FROM task_row WHERE task_id = ? ORDER BY row_no", (task_id,)
        ).fetchall()
        return [json.loads(r["values_json"]) for r in rows]

    def get_lineage(self, task_id: str) -> list[dict]:
        self.get_task(task_id)
        rows = self.conn.execute(
            "SELECT * FROM task_column WHERE task_id = ? ORDER BY ordinal", (task_id,)
        ).fetchall()
        return [
            {
                "target": r["target_column"],
                "source_column": r["source_column"],
                "header_line": r["header_line"],
                "provenance": json.loads(r["provenance"]) if r["provenance"] else None,
            }
            for r in rows
        ]

    def get_params(self, task_id: str) -> dict:
        self.get_task(task_id)
        row = self.conn.execute("SELECT params_json FROM task WHERE task_id = ?", (task_id,)).fetchone()
        return json.loads(row["params_json"])

    def get_param_sources(self, task_id: str) -> list[dict]:
        rows = self.conn.execute(
            "SELECT param, path, prov_json FROM task_param_source WHERE task_id = ? ORDER BY param",
            (task_id,),
        ).fetchall()
        return [
            {
                "param": r["param"],
                "path": r["path"],
                "provenance": json.loads(r["prov_json"]) if r["prov_json"] else None,
            }
            for r in rows
        ]

    def get_partition_rows(self, task_id: str, key_value: str) -> list[dict[str, str]]:
        row = self.conn.execute(
            "SELECT row_nos FROM task_partition WHERE task_id = ? AND key_value = ?",
            (task_id, key_value),
        ).fetchone()
        if row is None:
            raise CatalogError(f"任务 {task_id!r} 无分区值 {key_value!r}")
        wanted = set(json.loads(row["row_nos"]))
        all_rows = self.get_rows(task_id)
        return [r for i, r in enumerate(all_rows) if i in wanted]
