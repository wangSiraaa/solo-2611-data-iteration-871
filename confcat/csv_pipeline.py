"""CSV 数据转换模块：读取本地 CSV，按配置归一化为关系表。

与配置的合同
============
* ``column_mapping``：``目标列 -> 原 CSV 列``，目标列顺序即输出表列序；
  原列缺失、多个目标列映射到同一原列（列映射冲突）都拒绝整次任务；
* ``missing_value``：``token``（命中替换，默认空串）与 ``default``
  （整列缺失/全部缺失时兜底）；
* ``partition_by``：归一化后按分区键取值分组，键值缺失即拒绝整次任务。

输出的每一列都保留：目标列名、原 CSV 列名、列映射配置来源（Provenance）
以及原 CSV 表头行号，满足“成功表保留每列对应配置来源和原 CSV 列”。
"""

from __future__ import annotations

import csv
from dataclasses import dataclass
from pathlib import Path

from .errors import TaskRejected
from .model import Provenance


@dataclass(frozen=True)
class ColumnLineage:
    target: str
    source_column: str
    header_line: int  # 原 CSV 表头行号（csv.reader 行号，表头为 1）
    provenance: Provenance | None  # column_mapping 中该目标键的来源证据


@dataclass(frozen=True)
class MissingRule:
    token: str = ""  # 单元格等于该 token（含空串）视为缺失
    default: str | None = None  # 缺失时的兜底值；None 表示保留原 token


@dataclass
class NormalizedTable:
    columns: tuple[str, ...]
    rows: list[dict[str, str]]
    lineage: tuple[ColumnLineage, ...]
    partitions: dict[str, list[int]]  # 分区键值 -> 行号（0 基）
    source_csv: str
    header_line: int

    def column(self, name: str) -> ColumnLineage:
        for c in self.lineage:
            if c.target == name:
                return c
        raise KeyError(name)


def read_csv_header(path: str | Path) -> tuple[list[str], int]:
    with Path(path).open("r", encoding="utf-8", newline="") as fh:
        reader = csv.reader(fh)
        try:
            header = next(reader)
        except StopIteration:
            raise TaskRejected("CSV 文件为空，缺少表头", reason="empty-csv")
        header = [h.strip() for h in header]
        return header, reader.line_num


def normalize(
    csv_path: str | Path,
    column_mapping: dict[str, str],
    mapping_prov: dict[str, Provenance | None],
    missing: MissingRule | None = None,
    partition_by: tuple[str, ...] = (),
) -> NormalizedTable:
    """执行完整归一化；任何合同违规都抛 :class:`TaskRejected`（整次拒绝）。"""
    missing = missing or MissingRule()
    path = Path(csv_path)
    if not path.exists():
        raise TaskRejected(f"CSV 文件不存在: {path}", reason="csv-not-found")

    with path.open("r", encoding="utf-8", newline="") as fh:
        reader = csv.reader(fh)
        try:
            raw_header = next(reader)
        except StopIteration:
            raise TaskRejected("CSV 文件为空，缺少表头", reason="empty-csv")
        header_line = reader.line_num
        header = [h.strip() for h in raw_header]
        if len(set(header)) != len(header):
            raise TaskRejected(f"CSV 表头存在重复列: {header}", reason="duplicate-csv-column")

        # 1) 列映射校验：原列必须存在；同一原列不得被多个目标列占用
        targets = tuple(column_mapping.keys())
        used_sources: dict[str, str] = {}
        lineage: list[ColumnLineage] = []
        for target in targets:
            source = column_mapping[target]
            if source not in header:
                raise TaskRejected(
                    f"目标列 {target!r} 映射的原 CSV 列 {source!r} 不存在；表头为 {header}",
                    path=("column_mapping", target),
                    reason="missing-source-column",
                )
            if source in used_sources:
                raise TaskRejected(
                    f"列映射冲突：原列 {source!r} 同时映射给 {used_sources[source]!r} 与 {target!r}",
                    path=("column_mapping", target),
                    reason="column-mapping-conflict",
                )
            used_sources[source] = target
            lineage.append(
                ColumnLineage(
                    target=target,
                    source_column=source,
                    header_line=header_line,
                    provenance=mapping_prov.get(target),
                )
            )

        unknown_part = [p for p in partition_by if p not in targets]
        if unknown_part:
            raise TaskRejected(
                f"分区键 {unknown_part} 不在列映射目标列 {list(targets)} 中",
                path=("partition_by",),
                reason="unknown-partition-key",
            )

        # 2) 逐行归一化（原列按表头索引取值，列序不敏感）
        index = {name: i for i, name in enumerate(header)}
        rows: list[dict[str, str]] = []
        seen_missing_all: dict[str, bool] = {t: True for t in targets}
        for row in reader:
            if not row or all(cell.strip() == "" for cell in row):
                continue  # 跳过纯空行
            normalized: dict[str, str] = {}
            for target in targets:
                source = column_mapping[target]
                col = index[source]
                raw = row[col] if col < len(row) else ""
                if raw == missing.token:
                    if missing.default is not None:
                        normalized[target] = missing.default
                    else:
                        normalized[target] = raw  # 无 default：保留原缺失 token
                else:
                    seen_missing_all[target] = normalized[target] = raw
            rows.append(normalized)

        # 整列在数据区从未出现有效值时，default 兜底（若配置提供）
        if missing.default is not None:
            for target in targets:
                if seen_missing_all[target]:
                    for row in rows:
                        if row[target] == missing.token:
                            row[target] = missing.default

        # 3) 分区键：键值不得缺失
        partitions: dict[str, list[int]] = {}
        for row_no, row in enumerate(rows):
            for key in partition_by:
                value = row[key]
                if value == missing.token and missing.default is None:
                    raise TaskRejected(
                        f"第 {row_no + 2} 行分区键 {key!r} 缺失",
                        path=("partition_by", key),
                        reason="partition-key-missing",
                    )
                partitions.setdefault(value, []).append(row_no)

    return NormalizedTable(
        columns=targets,
        rows=rows,
        lineage=tuple(lineage),
        partitions=partitions,
        source_csv=str(path),
        header_line=header_line,
    )
