"""消费仓需求计算：按**已登记的换算约定**，从真实 CSV 任务明细计算一批任务
同时需要的分拣工时（hours）与托盘位（pallets）。

换算约定（登记在同一 YAML 产品的 ``conversion`` 段，随版本快照固定）
=====================================================================
::

    conversion:
      key_column: channel          # 明细行按该列取值选择换算行
      defaults: {hours_per_row: 1, pallets_per_row: 1}
      rules:                       # key_column 取值 -> 每行换算
        web:  {hours_per_row: 2, pallets_per_row: 2}
        app:  {hours_per_row: 1, pallets_per_row: 1}
      batch:
        pallets_divisor: 3         # 每满 N 行可把托位小计向下归并一次（可选）

计算合同
========
* 逐行计算后再汇总，两种资源各自独立累计；命中不到规则的键值走 ``defaults``，
  连 defaults 都没登记的键值 -> 整批拒绝（不允许悄悄按 0 或 1 估）；
* 工时为整数工时；托位支持可选的批量归并（整除向下取整后加余数），
  全部公式与取整路径在结果里逐行可复核；
* 数据来自消费仓已落库的真实任务明细（``TaskRepository.get_rows``），
  重开任务仓后同一任务 + 同一版本配置必然算出同一需求。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .errors import QuotaConfigError, TaskRejected
from .model import MatMapping, MatNode, MatScalar, MaterializedDocument

from .quota import CONVERSION_ROOT


@dataclass(frozen=True)
class ConversionRule:
    hours_per_row: int
    pallets_per_row: int


@dataclass(frozen=True)
class RowDemand:
    row_no: int
    key_value: str
    hours: int
    pallets: int
    matched_rule: str  # "rule:<key>" | "default"


@dataclass
class BatchDemand:
    task_id: str
    row_count: int
    hours: int
    pallets: int
    rows: list[RowDemand] = field(default_factory=list)
    pallets_divisor: int | None = None

    def as_dict(self) -> dict:
        return {
            "task_id": self.task_id,
            "row_count": self.row_count,
            "hours": self.hours,
            "pallets": self.pallets,
            "pallets_divisor": self.pallets_divisor,
            "rows": [r.__dict__ for r in self.rows],
        }


def _as_int(node: MatNode, path: tuple) -> int:
    if not isinstance(node, MatScalar) or isinstance(node.value, bool) or not isinstance(node.value, int):
        raise QuotaConfigError(f"/{'/'.join(map(str, path))} 必须是整数", path, "bad-type")
    if node.value < 0:
        raise QuotaConfigError(f"/{'/'.join(map(str, path))} 不能为负", path, "bad-value")
    return node.value


def load_conversion(doc: MaterializedDocument) -> tuple[str, ConversionRule, dict[str, ConversionRule], int | None]:
    """从物化文档读取已登记换算约定；合同违规在发布/任务期即拒绝。"""
    try:
        conv = doc.find(CONVERSION_ROOT)
    except (KeyError, IndexError):
        raise QuotaConfigError("文档缺少 conversion 换算约定", CONVERSION_ROOT, "missing-conversion")
    if not isinstance(conv, MatMapping):
        raise QuotaConfigError("conversion 必须是映射", CONVERSION_ROOT, "bad-type")

    if "key_column" not in conv.entries:
        raise QuotaConfigError("conversion 缺少 key_column", CONVERSION_ROOT + ("key_column",), "missing-field")
    key_node = conv.entries["key_column"]
    if not isinstance(key_node, MatScalar) or not isinstance(key_node.value, str):
        raise QuotaConfigError("conversion.key_column 必须是字符串", CONVERSION_ROOT + ("key_column",), "bad-type")
    key_column = key_node.value

    def read_rule(node: MatNode, path: tuple) -> ConversionRule:
        if not isinstance(node, MatMapping):
            raise QuotaConfigError(f"/{'/'.join(map(str, path))} 必须是映射", path, "bad-type")
        return ConversionRule(
            hours_per_row=_as_int(node.entries["hours_per_row"], path + ("hours_per_row",)),
            pallets_per_row=_as_int(node.entries["pallets_per_row"], path + ("pallets_per_row",)),
        )

    if "defaults" not in conv.entries:
        raise QuotaConfigError("conversion 缺少 defaults 兜底行", CONVERSION_ROOT + ("defaults",), "missing-field")
    defaults = read_rule(conv.entries["defaults"], CONVERSION_ROOT + ("defaults",))

    rules: dict[str, ConversionRule] = {}
    if "rules" in conv.entries:
        rules_node = conv.entries["rules"]
        if not isinstance(rules_node, MatMapping):
            raise QuotaConfigError("conversion.rules 必须是映射", CONVERSION_ROOT + ("rules",), "bad-type")
        for key, child in rules_node.entries.items():
            if not isinstance(key, str):
                raise QuotaConfigError("conversion.rules 的键必须是字符串", CONVERSION_ROOT + ("rules",), "bad-type")
            rules[key] = read_rule(child, CONVERSION_ROOT + ("rules", key))

    divisor: int | None = None
    if "batch" in conv.entries:
        batch_node = conv.entries["batch"]
        if not isinstance(batch_node, MatMapping) or "pallets_divisor" not in batch_node.entries:
            raise QuotaConfigError(
                "conversion.batch 需要 pallets_divisor", CONVERSION_ROOT + ("batch",), "missing-field"
            )
        divisor = _as_int(
            batch_node.entries["pallets_divisor"], CONVERSION_ROOT + ("batch", "pallets_divisor")
        )
        if divisor <= 0:
            raise QuotaConfigError(
                "pallets_divisor 必须为正整数",
                CONVERSION_ROOT + ("batch", "pallets_divisor"),
                "bad-value",
            )
    return key_column, defaults, rules, divisor


def compute_batch_demand(
    task_id: str,
    rows: list[dict[str, str]],
    key_column: str,
    defaults: ConversionRule,
    rules: dict[str, ConversionRule],
    pallets_divisor: int | None = None,
) -> BatchDemand:
    """对一批真实任务明细计算两种资源的同时需求量（逐行可复核）。"""
    if not rows:
        raise TaskRejected("任务明细为空，无法计算批次需求", reason="empty-batch")
    details: list[RowDemand] = []
    total_hours = 0
    raw_pallets = 0
    for row_no, row in enumerate(rows):
        if key_column not in row:
            raise TaskRejected(
                f"第 {row_no} 行缺少换算键列 {key_column!r}",
                path=("conversion", "key_column"),
                reason="conversion-key-missing",
            )
        key_value = row[key_column]
        if key_value in rules:
            rule = rules[key_value]
            matched = f"rule:{key_value}"
        elif defaults is not None:
            rule = defaults
            matched = "default"
        else:
            raise TaskRejected(
                f"键值 {key_value!r} 未登记换算规则且无 defaults",
                path=("conversion", "rules",),
                reason="unregistered-conversion-key",
            )
        total_hours += rule.hours_per_row
        raw_pallets += rule.pallets_per_row
        details.append(
            RowDemand(
                row_no=row_no,
                key_value=key_value,
                hours=rule.hours_per_row,
                pallets=rule.pallets_per_row,
                matched_rule=matched,
            )
        )

    # 批量归并：每 divisor 行的托位向下合并一次，余数照计；未登记则原值
    if pallets_divisor:
        pallets = raw_pallets // pallets_divisor + raw_pallets % pallets_divisor
    else:
        pallets = raw_pallets
    return BatchDemand(
        task_id=task_id,
        row_count=len(rows),
        hours=total_hours,
        pallets=pallets,
        rows=details,
        pallets_divisor=pallets_divisor,
    )


def demand_for_task(repo, catalog, task_id: str) -> BatchDemand:
    """端到端：按任务自己绑定的产品版本快照取换算约定，对其真实明细算需求。

    重开任务仓/目录后调用结果不变——配置与明细都是当时落库的快照。
    """
    summary = repo.get_task(task_id)
    doc = repo._doc_snapshot(summary.product, summary.doc_name, summary.version)
    key_column, defaults, rules, divisor = load_conversion(doc)
    rows = repo.get_rows(task_id)
    return compute_batch_demand(task_id, rows, key_column, defaults, rules, divisor)
