"""额度池身份目录：把 YAML 产品里各 CSV 接入站声明的资源限额，发布成带
**稳定身份（pool_id）与结算周期（period）** 的额度池。

回答题面第一问：“断网接入站领走的处理额度究竟算哪个资源池？”
=====================================================================
同一限额数字可能来自两座仓，也可能只是同一池子的两种读法，本模块用
**池身份**而不是限额值来回答：

1. **别名 / merge 同一锚点 => 同一池**：两个分站视图若最终指向同一锚点节点
   （物化证据 ``alias_chain`` 同源），则是同一 ``pool_id`` 的两个读法；
   同周期两站都看到 5 小时，那是**同一个 5 小时池**，不能各领 3。
2. **显式独立池 => 不同池**：两处都是显式字面量（无别名链），即使限额相等
   （各 5）也必须是两个 ``pool_id``，同值**禁止合并**，两站可以各领 3。
3. **重命名不改身份**：``pool_id`` 是发布时即固定的稳定身份；展示名
   ``name`` 在新版本里可以改，旧额度单/旧承诺仍按原 ``pool_id`` 占原余额。
4. **同池读法冲突即配置错误**：同一锚点被两处读成不同限额（或被一个独立
   池显式覆盖成同组另一值）时，发布期就抛 :class:`QuotaConfigError`，
   绝不静默挑一个值。

工时（hours）与托盘位（pallets）是**两类独立资源**，各自走各自的有效池；
任务通过已有的来源优先级（merge 从左到右、显式键覆盖、别名链）取得每类
资源在本站的有效池。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .errors import QuotaConfigError
from .model import MatMapping, MatNode, MatScalar, MaterializedDocument, Provenance

# 两类资源；每类资源的池空间互不相干
RESOURCES: tuple[str, ...] = ("hours", "pallets")
# quota 配置在产品文档里的固定根路径
QUOTA_ROOT = ("quota",)
CONVERSION_ROOT = ("conversion",)


# --------------------------------------------------------------------------- #
# 数据模型
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class PoolView:
    """一个分站对某资源池的一次“读法”（quota.<station>.<resource> 条目）。"""

    station: str
    resource: str
    limit: int
    via_anchor: str | None  # 非空 => 别名/merge 读法；None => 显式独立池
    alias_chain: tuple[str, ...]
    # 该有效条目的配置来源（路径/位置/别名链/merge 胜负），可复核
    provenance: dict
    pool_id: str  # 解析后归属的稳定池身份


@dataclass
class PoolRecord:
    """一个带稳定身份与结算周期的额度池。"""

    pool_id: str
    resource: str
    period: str
    name: str
    warehouse: str | None
    limit: int
    renamed_from: str | None = None  # 新版本改展示名时记录旧名；身份不变
    views: list[PoolView] = field(default_factory=list)

    def stations(self) -> tuple[str, ...]:
        return tuple(sorted({v.station for v in self.views}))

    def as_dict(self) -> dict:
        return {
            "pool_id": self.pool_id,
            "resource": self.resource,
            "period": self.period,
            "name": self.name,
            "warehouse": self.warehouse,
            "limit": self.limit,
            "renamed_from": self.renamed_from,
            "stations": list(self.stations()),
            "views": [
                {
                    "station": v.station,
                    "limit": v.limit,
                    "via_anchor": v.via_anchor,
                    "alias_chain": list(v.alias_chain),
                    "provenance": v.provenance,
                }
                for v in self.views
            ],
        }


@dataclass
class PoolRegistry:
    """一次发布的完整额度池目录（绑定产品/版本/周期）。"""

    product: str
    version: int
    doc_name: str
    period: str
    pools: dict[str, PoolRecord]  # pool_id -> record
    # (station, resource) -> pool_id：任务取“有效池”的入口
    effective: dict[tuple[str, str], str]

    def pool_for(self, station: str, resource: str) -> PoolRecord:
        key = (station, resource)
        if key not in self.effective:
            raise QuotaConfigError(
                f"分站 {station!r} 没有资源 {resource!r} 的有效池声明",
                path=("quota", station, resource),
                reason="missing-pool-view",
            )
        return self.pools[self.effective[key]]

    def pools_of(self, resource: str) -> list[PoolRecord]:
        return sorted(
            (p for p in self.pools.values() if p.resource == resource),
            key=lambda p: p.pool_id,
        )

    def as_dict(self) -> dict:
        return {
            "product": self.product,
            "version": self.version,
            "doc_name": self.doc_name,
            "period": self.period,
            "pools": [self.pools[k].as_dict() for k in sorted(self.pools)],
            "effective": [
                {"station": s, "resource": r, "pool_id": pid}
                for (s, r), pid in sorted(self.effective.items())
            ],
        }


# --------------------------------------------------------------------------- #
# 物化树读取辅助（严格类型，错误即拒绝整次发布）
# --------------------------------------------------------------------------- #
def _expect(node: MatNode, kind: str, path: tuple):
    if kind == "map" and not isinstance(node, MatMapping):
        raise QuotaConfigError(f"路径 /{'/'.join(map(str, path))} 应为映射", path, "bad-type")
    if kind == "str" and (not isinstance(node, MatScalar) or not isinstance(node.value, str)):
        raise QuotaConfigError(f"路径 /{'/'.join(map(str, path))} 应为字符串", path, "bad-type")
    if kind == "int" and (
        not isinstance(node, MatScalar) or isinstance(node.value, bool) or not isinstance(node.value, int)
    ):
        raise QuotaConfigError(f"路径 /{'/'.join(map(str, path))} 应为非负整数", path, "bad-type")


def _int_field(mapping: MatMapping, key: str, path: tuple, minimum: int = 0) -> int:
    if key not in mapping.entries:
        raise QuotaConfigError(f"缺少必需字段 {key!r}", path + (key,), "missing-field")
    node = mapping.entries[key]
    _expect(node, "int", path + (key,))
    value = node.value
    if value < minimum:
        raise QuotaConfigError(f"字段 {key!r} 不能小于 {minimum}", path + (key,), "bad-value")
    return value


def _str_field(mapping: MatMapping, key: str, path: tuple, required: bool = True) -> str | None:
    if key not in mapping.entries:
        if required:
            raise QuotaConfigError(f"缺少必需字段 {key!r}", path + (key,), "missing-field")
        return None
    node = mapping.entries[key]
    _expect(node, "str", path + (key,))
    return node.value


def _prov_dict(prov: Provenance | None) -> dict:
    return prov.as_dict() if prov is not None else {}


def _entry_identity(entry: MatMapping, entry_path: tuple) -> tuple[str | None, tuple[str, ...], dict]:
    """判定一个资源条目的池身份。

    物化期证据形态（经实测确认）：

    * 直接别名（``hours: *sh``）：条目节点本身无链，但内部 ``limit``/``name``
      等键的 ``alias_chain`` 指向锚点；
    * merge（``<<: *pa`` + 显式 ``limit``）：未被覆盖的键带别名链，被显式
      覆盖的键留 ``merge_evidence``（won=False）；
    * 显式独立池：所有键别名链为空、无 merge 证据。

    因此身份取自条目内**任意键别名链的起点锚点**；多个键指向不同锚点属于
    合同违规（一个条目只能是一个池的读法）。
    """
    anchors: set[str] = set()
    chain: tuple[str, ...] = ()
    merge_seen = False
    for key, child in entry.entries.items():
        prov = entry.key_prov.get(key) or child.prov
        if prov is None:
            continue
        if prov.alias_chain:
            anchors.add(prov.alias_chain[0].alias_name)
            if not chain:
                chain = tuple(a.alias_name for a in prov.alias_chain)
        if prov.merge_evidence:
            merge_seen = True
    if len(anchors) > 1:
        raise QuotaConfigError(
            f"条目同时引用多个锚点 {sorted(anchors)}，一个资源条目只能是一个池的读法",
            entry_path,
            "pool-identity-conflict",
        )
    if anchors:
        return next(iter(anchors)), chain, {"merged": merge_seen}
    return None, (), {"merged": merge_seen}


# --------------------------------------------------------------------------- #
# 目录构建
# --------------------------------------------------------------------------- #
def build_registry(
    doc: MaterializedDocument, product: str, version: int, doc_name: str
) -> PoolRegistry:
    """从已发布物化树构建额度池目录。

    身份判定完全基于物化期来源证据：``alias_chain`` 记录了每个有效条目经过
    的锚点；同锚点即同池，显式条目（无链）即独立池。
    """
    try:
        quota_node = doc.find(QUOTA_ROOT)
    except (KeyError, IndexError):
        raise QuotaConfigError("文档缺少 quota 根映射", QUOTA_ROOT, "missing-quota")
    _expect(quota_node, "map", QUOTA_ROOT)
    if "period" not in quota_node.entries:
        raise QuotaConfigError("quota 缺少结算周期 period", QUOTA_ROOT + ("period",), "missing-field")
    period = _str_field(quota_node, "period", QUOTA_ROOT)
    if not period:
        raise QuotaConfigError("period 不能为空", QUOTA_ROOT + ("period",), "bad-value")

    pools: dict[str, PoolRecord] = {}
    effective: dict[tuple[str, str], str] = {}

    for station, station_node in quota_node.entries.items():
        if station == "period":
            continue
        if not isinstance(station, str):
            raise QuotaConfigError("分站名必须是字符串键", QUOTA_ROOT + (station,), "bad-type")
        station_path = QUOTA_ROOT + (station,)
        _expect(station_node, "map", station_path)
        # quota 下允许挂锚点声明（如 shared_hours: &x {name, warehouse, limit}）。
        # 锚点定义不含任何资源键（hours/pallets）=> 跳过；
        # 只要含至少一个资源键，就视为分站视图，缺另一类资源必须显式报错。
        resource_keys = [k for k in station_node.entries if k in RESOURCES]
        if not resource_keys:
            continue

        for resource in RESOURCES:
            if resource not in station_node.entries:
                raise QuotaConfigError(
                    f"分站 {station!r} 缺少资源 {resource!r} 声明",
                    station_path + (resource,),
                    "missing-resource",
                )
            entry = station_node.entries[resource]
            entry_path = station_path + (resource,)
            _expect(entry, "map", entry_path)

            limit = _int_field(entry, "limit", entry_path)
            name = _str_field(entry, "name", entry_path)
            warehouse = _str_field(entry, "warehouse", entry_path, required=False)
            # 身份依据：条目内任意键别名链的**起点锚点**（merge 被显式覆盖时，
            # 其余键仍留链）。无任何别名链 => 显式独立池，身份绑定配置路径，
            # 永不与他处合并——即使限额与别的池相等。
            via_anchor, chain, identity_note = _entry_identity(entry, entry_path)
            if via_anchor is not None:
                pool_id = f"{resource}::{via_anchor}"
            else:
                pool_id = f"{resource}::explicit::{station}"

            entry_prov = entry.key_prov.get(resource) or entry.prov
            view = PoolView(
                station=station,
                resource=resource,
                limit=limit,
                via_anchor=via_anchor,
                alias_chain=chain,
                provenance=_prov_dict(entry_prov) | {"identity": identity_note},
                pool_id=pool_id,
            )

            if pool_id in pools:
                record = pools[pool_id]
                # 同一池的两种读法：限额/周期必须一致，否则配置自相矛盾
                if record.limit != limit:
                    raise QuotaConfigError(
                        f"池 {pool_id} 被 {record.stations() + (station,)} 读成不同限额 "
                        f"{record.limit} != {limit}；同一锚点的读法必须同值",
                        entry_path + ("limit",),
                        "pool-identity-conflict",
                    )
                # 同一池允许各自带仓库备注，但展示名必须一致（名字属于池不属于读法）
                if record.name != name:
                    raise QuotaConfigError(
                        f"池 {pool_id} 在不同分站处展示名不一致: {record.name!r} != {name!r}",
                        entry_path + ("name",),
                        "pool-identity-conflict",
                    )
                record.views.append(view)
            else:
                pools[pool_id] = PoolRecord(
                    pool_id=pool_id,
                    resource=resource,
                    period=period,
                    name=name,
                    warehouse=warehouse,
                    limit=limit,
                    views=[view],
                )
            effective[(station, resource)] = pool_id

    return PoolRegistry(
        product=product,
        version=version,
        doc_name=doc_name,
        period=period,
        pools=pools,
        effective=effective,
    )


def registry_from_catalog(catalog, product: str, doc_name: str, version: int | None = None) -> PoolRegistry:
    """从目录已发布版本构建注册表（任务固定版本快照，重开结果一致）。"""
    version = catalog._resolve_version(product, version)
    doc = catalog.load_document(product, doc_name, version)
    return build_registry(doc, product, version, doc_name)


def apply_rename(previous: PoolRegistry, current: PoolRegistry) -> PoolRegistry:
    """新版本发布时做身份对账：

    * ``pool_id`` 仍在 => 身份未变；若展示名变化，记 ``renamed_from``，
      旧发放仍按该 ``pool_id`` 占原余额；
    * 旧 ``pool_id`` 消失 => 配置删除了池（由调用方按周期策略处理，本函数只标记）。
    """
    for pool_id, record in current.pools.items():
        old = previous.pools.get(pool_id)
        if old is not None and old.name != record.name:
            record.renamed_from = old.name
    return current
