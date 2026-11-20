"""解析原始图（raw graph）与物化有效树（materialized tree）的数据模型。

设计要点
--------
1. ``RawNode`` 保留文档文本结构：锚点节点可被多个别名共享，形成共享子图；
2. 别名不在解析期拷贝目标，只记录目标锚点名，保留“共享子结构”事实；
3. 物化阶段（:mod:`confcat.materialize`）按 merge 合同生成独立的有效树
   ``MatNode``，每个有效键附带 :class:`Provenance` 来源证据；
4. 键只允许 ``str`` 与 ``int``，二者互不同名（整数 1 与字符串 "1" 是两个键）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Union

# 本期允许的两类映射键
ConfigKey = Union[str, int]


# --------------------------------------------------------------------------- #
# 来源证据
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class Mark:
    """文档内文本位置。"""

    line: int  # 1 基行号，物理文件行号
    column: int  # 0 基列号
    doc_index: int = 0  # 多文档流中的文档序号（0 基）

    def as_dict(self) -> dict:
        return {"line": self.line, "column": self.column, "doc_index": self.doc_index}


@dataclass(frozen=True)
class AliasEvent:
    """一次别名解引用事件；多个事件按展开顺序拼成别名链。"""

    alias_name: str
    alias_mark: Mark
    anchor_mark: Mark

    def as_dict(self) -> dict:
        return {
            "alias": self.alias_name,
            "alias_at": self.alias_mark.as_dict(),
            "anchor_at": self.anchor_mark.as_dict(),
        }


@dataclass(frozen=True)
class MergeEvidence:
    """merge 覆盖证据：一个候选键来自哪个 merge 源、是否被显式键覆盖。"""

    source_index: int  # merge 序列从左到右的序号；单映射 merge 为 0
    source_kind: str  # "merge-map" | "merge-seq-item"
    key_mark: Mark  # 该键在 merge 源中的定义位置
    won: bool  # 该候选是否实际胜出
    overridden_by: Mark | None  # 未胜出时，被哪个位置的显式键/更早候选覆盖

    def as_dict(self) -> dict:
        return {
            "source_index": self.source_index,
            "source_kind": self.source_kind,
            "key_at": self.key_mark.as_dict(),
            "won": self.won,
            "overridden_by": self.overridden_by.as_dict() if self.overridden_by else None,
        }


@dataclass(frozen=True)
class Provenance:
    """一个有效键/节点的完整来源信息。"""

    path: tuple  # 从文档根到该节点的有效路径（str/int 混合）
    value_mark: Mark  # 胜出值定义位置
    key_mark: Mark  # 胜出键定义位置（merge 来的键是其在源映射中的键位置）
    explicit: bool  # 是否为当前映射显式键（显式键始终覆盖 merge）
    alias_chain: tuple[AliasEvent, ...] = ()
    merge_evidence: tuple[MergeEvidence, ...] = ()

    def as_dict(self) -> dict:
        return {
            "path": list(self.path),
            "explicit": self.explicit,
            "value_at": self.value_mark.as_dict(),
            "key_at": self.key_mark.as_dict(),
            "alias_chain": [e.as_dict() for e in self.alias_chain],
            "merge_evidence": [e.as_dict() for e in self.merge_evidence],
        }


# --------------------------------------------------------------------------- #
# 解析期原始节点：共享锚点图
# --------------------------------------------------------------------------- #
@dataclass
class RawNode:
    """原始节点基类。``anchor`` 为该节点自身被声明的锚点名（可空）。"""

    mark: Mark
    anchor: str | None = None
    anchor_mark: Mark | None = None


@dataclass
class RawScalar(RawNode):
    value: Any = None  # str | int | bool | None
    quoted: bool = False  # 是否引号字符串（引号 true 仍是字符串）


@dataclass
class RawMapping(RawNode):
    # 保留文本顺序；键为 (类型, 值) 二元组以区分 1 与 "1"
    entries: list[tuple[ConfigKey, Mark, RawNode]] = field(default_factory=list)

    def get(self, key: ConfigKey) -> RawNode | None:
        for k, _m, v in self.entries:
            if k == key and type(k) is type(key):
                return v
        return None


@dataclass
class RawSequence(RawNode):
    items: list[RawNode] = field(default_factory=list)


@dataclass
class RawAlias(RawNode):
    """别名节点：只记录目标锚点名，物化时才解引用。"""

    target: str = ""


@dataclass
class ParsedDocument:
    """单个 YAML 文档的解析结果。"""

    index: int
    root: RawNode
    anchors: dict[str, RawNode]
    source_text: str


# --------------------------------------------------------------------------- #
# 物化期有效节点：独立树 + 每键来源
# --------------------------------------------------------------------------- #
@dataclass
class MatNode:
    prov: Provenance | None = None


@dataclass
class MatScalar(MatNode):
    value: Any = None  # str | int | bool | None


@dataclass
class MatMapping(MatNode):
    # 键同样以 Python 原生值承载；1 与 "1" 天然是两个 dict 键
    entries: dict[ConfigKey, MatNode] = field(default_factory=dict)
    key_prov: dict[ConfigKey, Provenance] = field(default_factory=dict)


@dataclass
class MatSequence(MatNode):
    items: list[MatNode] = field(default_factory=list)


@dataclass
class MaterializedDocument:
    index: int
    root: MatNode
    anchor_names: tuple[str, ...]
    source_hash: str

    def find(self, path: tuple) -> MatNode:
        """按有效路径取值；缺失或序列越界抛 KeyError/IndexError。"""
        node = self.root
        for part in path:
            if isinstance(node, MatMapping):
                if part not in node.entries:
                    raise KeyError(part)
                node = node.entries[part]
            elif isinstance(node, MatSequence):
                if not isinstance(part, int) or part >= len(node.items):
                    raise IndexError(part)
                node = node.items[part]
            else:
                raise KeyError(part)
        return node
