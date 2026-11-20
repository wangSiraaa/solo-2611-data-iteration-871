"""配置产品目录服务（SQLite 持久化，可独立用于人工检查）。

发布模型
========
* 一个 YAML 文件可包含多文档，按文档序号发布为同产品下的命名文档（默认 ``doc-0`` ...）；
* 每次 ``publish`` 生成新版本，旧版本完整保留；在途任务按其创建时的版本快照运行，
  因此“任务固定配置产品后，其他产品发布不影响在途任务”天然成立；
* 物化树与全量来源证据（位置/别名链/merge 覆盖证据）以 JSON 落库，
  并保存源文本哈希，重开目录后可回查并校验配置链未被篡改。
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .codec import decode_document, encode_document, parse_path, path_str, to_plain
from .errors import CatalogError
from .materialize import materialize_all
from .model import MaterializedDocument, Provenance
from .yaml_parser import parse_text

_SCHEMA = """
CREATE TABLE IF NOT EXISTS catalog_meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS product (
    product     TEXT NOT NULL,
    version     INTEGER NOT NULL,
    created_at  TEXT NOT NULL,
    source_path TEXT,
    digest      TEXT NOT NULL,
    PRIMARY KEY (product, version)
);
CREATE TABLE IF NOT EXISTS document (
    product        TEXT NOT NULL,
    version        INTEGER NOT NULL,
    doc_name       TEXT NOT NULL,
    doc_index      INTEGER NOT NULL,
    snapshot       TEXT NOT NULL,
    source_yaml    TEXT NOT NULL,
    PRIMARY KEY (product, version, doc_name),
    FOREIGN KEY (product, version) REFERENCES product(product, version)
);
"""


@dataclass
class ProductInfo:
    product: str
    version: int
    created_at: str
    source_path: str | None
    digest: str
    docs: tuple[str, ...]


@dataclass
class QueryResult:
    product: str
    version: int
    doc_name: str
    path: tuple
    value: Any
    provenance: Provenance | None


class Catalog:
    """命名配置产品目录。线程模型：单连接，按调用串行使用。"""

    def __init__(self, db_path: str | Path):
        self.db_path = str(db_path)
        self.conn = sqlite3.connect(self.db_path)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(_SCHEMA)
        self.conn.execute(
            "INSERT OR IGNORE INTO catalog_meta(key, value) VALUES('schema_version', '1')"
        )
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> "Catalog":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # ------------------------------------------------------------------ #
    def publish(
        self,
        product: str,
        yaml_text: str,
        source_path: str | None = None,
        doc_names: list[str] | None = None,
    ) -> ProductInfo:
        """发布一个新产品版本，返回版本信息。解析/物化失败则整次不写入。"""
        parsed = parse_text(yaml_text)
        materialized = materialize_all(parsed)
        if doc_names is not None and len(doc_names) != len(materialized):
            raise CatalogError("doc_names 数量与 YAML 文档数不一致")
        names = doc_names or [f"doc-{d.index}" for d in materialized]
        if len(set(names)) != len(names):
            raise CatalogError("文档名必须唯一")

        row = self.conn.execute(
            "SELECT COALESCE(MAX(version), 0) AS v FROM product WHERE product = ?", (product,)
        ).fetchone()
        next_version = int(row["v"]) + 1
        created_at = datetime.now(timezone.utc).isoformat()
        import hashlib

        digest = hashlib.sha256(yaml_text.encode("utf-8")).hexdigest()
        try:
            self.conn.execute(
                "INSERT INTO product(product, version, created_at, source_path, digest)"
                " VALUES(?, ?, ?, ?, ?)",
                (product, next_version, created_at, source_path, digest),
            )
            for name, mat in zip(names, materialized):
                self.conn.execute(
                    "INSERT INTO document(product, version, doc_name, doc_index, snapshot, source_yaml)"
                    " VALUES(?, ?, ?, ?, ?, ?)",
                    (
                        product,
                        next_version,
                        name,
                        mat.index,
                        json.dumps(encode_document(mat), ensure_ascii=False),
                        yaml_text,
                    ),
                )
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise
        return ProductInfo(product, next_version, created_at, source_path, digest, tuple(names))

    def publish_file(self, product: str, path: str | Path, doc_names: list[str] | None = None) -> ProductInfo:
        text = Path(path).read_text(encoding="utf-8")
        return self.publish(product, text, source_path=str(path), doc_names=doc_names)

    # ------------------------------------------------------------------ #
    def list_products(self) -> list[ProductInfo]:
        rows = self.conn.execute(
            "SELECT p.*, GROUP_CONCAT(d.doc_name) AS docs FROM product p"
            " LEFT JOIN document d ON d.product = p.product AND d.version = p.version"
            " GROUP BY p.product, p.version ORDER BY p.product, p.version"
        ).fetchall()
        result = []
        for r in rows:
            docs = tuple(sorted(r["docs"].split(","))) if r["docs"] else ()
            result.append(
                ProductInfo(r["product"], r["version"], r["created_at"], r["source_path"], r["digest"], docs)
            )
        return result

    def latest_version(self, product: str) -> int:
        row = self.conn.execute(
            "SELECT MAX(version) AS v FROM product WHERE product = ?", (product,)
        ).fetchone()
        if row["v"] is None:
            raise CatalogError(f"产品 {product!r} 不存在")
        return int(row["v"])

    def list_versions(self, product: str) -> list[int]:
        rows = self.conn.execute(
            "SELECT version FROM product WHERE product = ? ORDER BY version", (product,)
        ).fetchall()
        if not rows:
            raise CatalogError(f"产品 {product!r} 不存在")
        return [int(r["version"]) for r in rows]

    def list_docs(self, product: str, version: int | None = None) -> list[str]:
        version = self._resolve_version(product, version)
        rows = self.conn.execute(
            "SELECT doc_name FROM document WHERE product = ? AND version = ? ORDER BY doc_index",
            (product, version),
        ).fetchall()
        return [r["doc_name"] for r in rows]

    def _resolve_version(self, product: str, version: int | None) -> int:
        return version if version is not None else self.latest_version(product)

    def load_document(self, product: str, doc_name: str, version: int | None = None) -> MaterializedDocument:
        version = self._resolve_version(product, version)
        row = self.conn.execute(
            "SELECT snapshot FROM document WHERE product = ? AND version = ? AND doc_name = ?",
            (product, version, doc_name),
        ).fetchone()
        if row is None:
            raise CatalogError(f"文档 {doc_name!r} 不存在（产品 {product!r} v{version}）")
        return decode_document(json.loads(row["snapshot"]))

    def source_yaml(self, product: str, version: int | None = None) -> str:
        version = self._resolve_version(product, version)
        row = self.conn.execute(
            "SELECT source_yaml FROM document WHERE product = ? AND version = ? ORDER BY doc_index LIMIT 1",
            (product, version),
        ).fetchone()
        if row is None:
            raise CatalogError(f"产品 {product!r} v{version} 无文档")
        return row["source_yaml"]

    def verify(self, product: str, version: int | None = None) -> dict[str, str]:
        """重算源文本哈希并与发布记录比对，回查配置链完整性。"""
        version = self._resolve_version(product, version)
        rows = self.conn.execute(
            "SELECT doc_name, snapshot, source_yaml FROM document"
            " WHERE product = ? AND version = ? ORDER BY doc_index",
            (product, version),
        ).fetchall()
        import hashlib

        report = {}
        for r in rows:
            doc = decode_document(json.loads(r["snapshot"]))
            actual = hashlib.sha256(r["source_yaml"].encode("utf-8")).hexdigest()
            report[r["doc_name"]] = "ok" if actual == doc.source_hash else "digest-mismatch"
        return report

    # ------------------------------------------------------------------ #
    def query(
        self,
        product: str,
        doc_name: str,
        path: str | tuple,
        version: int | None = None,
    ) -> QueryResult:
        """查询指定文档路径的有效值及其来源证据。"""
        version = self._resolve_version(product, version)
        doc = self.load_document(product, doc_name, version)
        p = parse_path(path)
        try:
            node = doc.find(p)
        except (KeyError, IndexError):
            raise CatalogError(
                f"路径 {path_str(p)!r} 在产品 {product!r} v{version}/{doc_name} 中不存在"
            ) from None
        value = to_plain(node)
        return QueryResult(product, version, doc_name, p, value, node.prov)

    def query_typed(
        self,
        product: str,
        doc_name: str,
        path: str | tuple,
        expected: type | tuple[type, ...],
        version: int | None = None,
    ) -> QueryResult:
        """类型化查询：值类型不符即抛 CatalogError（bool 不视作 int）。"""
        result = self.query(product, doc_name, path, version)
        value = result.value
        if expected is int and isinstance(value, bool):
            raise CatalogError(f"路径 {path_str(result.path)!r} 期望 int，实际为 bool")
        if not isinstance(value, expected):
            names = (
                expected.__name__
                if isinstance(expected, type)
                else "/".join(t.__name__ for t in expected)
            )
            raise CatalogError(
                f"路径 {path_str(result.path)!r} 期望 {names}，实际为 {type(value).__name__}"
            )
        return result

    def export_plain(self, product: str, doc_name: str, version: int | None = None) -> Any:
        """导出普通 Python 视图（深拷贝），供人工检查或摘要展示。"""
        return to_plain(self.load_document(product, doc_name, version).root)
