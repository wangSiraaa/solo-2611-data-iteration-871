"""把解析期共享图物化为独立有效树，并产出每键来源证据。

merge 合同（严格按题面）
========================
1. ``<<`` 的值为一个映射，或一个“映射序列”；其他类型拒绝；
2. 序列按从左到右优先：第一个提供的键胜出，后面的只补缺；
3. 当前映射的**显式键始终覆盖** merge 所得，与 ``<<`` 在文本中的前后位置无关；
4. merge 只复制“键 -> 值引用”，**不递归合并**同名子映射（子映射整体被先到者占位）；
5. 字符串键相同才冲突：``1``（int）与 ``"1"``（str）是两个键，互不覆盖。

别名合同
========
* 解引用时逐跳记录 :class:`AliasEvent`，形成别名链；
* 别名链成环立即抛 :class:`CircularReferenceError`，深度超限抛 :class:`DepthLimitError`；
* 物化结果每个插入点都是独立子树（消费方修改不可能经共享别名影响其他任务），
  共享事实通过 raw graph 同一性与 alias_chain 证据保留。
"""

from __future__ import annotations

import hashlib
from typing import NamedTuple

from .errors import CircularReferenceError, DepthLimitError, MergeTypeError
from .yaml_parser import MERGE_KEY
from .model import (
    AliasEvent,
    Mark,
    MatMapping,
    MatNode,
    MatScalar,
    MatSequence,
    MaterializedDocument,
    MergeEvidence,
    ParsedDocument,
    RawAlias,
    RawMapping,
    RawNode,
    RawScalar,
    RawSequence,
    Provenance,
)

DEFAULT_MAX_DEPTH = 50


class _Candidate(NamedTuple):
    key_mark: Mark
    value: RawNode
    source_index: int
    source_kind: str
    chain: tuple[AliasEvent, ...]


class _Effective(NamedTuple):
    key: object
    key_mark: Mark
    value: RawNode
    chain: tuple[AliasEvent, ...]


class Materializer:
    def __init__(self, doc: ParsedDocument, max_depth: int = DEFAULT_MAX_DEPTH):
        self.doc = doc
        self.anchors = doc.anchors
        self.max_depth = max_depth

    def materialize(self) -> MaterializedDocument:
        root = self._mat(self.doc.root, path=(), chain=(), active=frozenset(), depth=1)
        return MaterializedDocument(
            index=self.doc.index,
            root=root,
            anchor_names=tuple(sorted(self.anchors)),
            source_hash=hashlib.sha256(self.doc.source_text.encode("utf-8")).hexdigest(),
        )

    # ------------------------------------------------------------------ #
    def _deref(self, node: RawNode, chain: tuple[AliasEvent, ...], active: frozenset[str]):
        """沿别名逐跳解引用，累积别名链并检测循环。"""
        while isinstance(node, RawAlias):
            name = node.target
            if name in active:
                raise CircularReferenceError(
                    f"别名链在锚点 {name!r} 处形成循环引用（文档 {self.doc.index}）"
                )
            target = self.anchors.get(name)
            if target is None:  # 解析器已拦截，这里属于防御
                raise CircularReferenceError(f"别名 *{name} 目标缺失")
            chain = chain + (
                AliasEvent(
                    alias_name=name,
                    alias_mark=node.mark,
                    anchor_mark=target.anchor_mark or target.mark,
                ),
            )
            node = target
            active = active | {name}
        return node, chain, active

    def _mat(
        self,
        node: RawNode,
        path: tuple,
        chain: tuple[AliasEvent, ...],
        active: frozenset[str],
        depth: int,
    ) -> MatNode:
        if depth > self.max_depth:
            raise DepthLimitError(f"物化嵌套深度超过 {self.max_depth} 限制（路径 {path}）")
        node, chain, active = self._deref(node, chain, active)

        if isinstance(node, RawScalar):
            return MatScalar(prov=self._prov(path, node.mark, node.mark, True, chain), value=node.value)
        if isinstance(node, RawSequence):
            seq = MatSequence(prov=self._prov(path, node.mark, node.mark, True, chain))
            for idx, item in enumerate(node.items):
                seq.items.append(self._mat(item, path + (idx,), chain, active, depth + 1))
            return seq
        if isinstance(node, RawMapping):
            return self._mat_mapping(node, path, chain, active, depth)
        raise MergeTypeError(f"无法物化的节点类型 {type(node).__name__}")

    # ------------------------------------------------------------------ #
    def _mat_mapping(
        self,
        node: RawMapping,
        path: tuple,
        chain: tuple[AliasEvent, ...],
        active: frozenset[str],
        depth: int,
    ) -> MatMapping:
        result = MatMapping(prov=self._prov(path, node.mark, node.mark, True, chain))

        explicit: list[tuple] = []  # (key, key_mark, raw_value)
        merge_nodes: list[RawNode] = []
        for key, key_mark, value in node.entries:
            if key is MERGE_KEY:
                merge_nodes.append(value)
            else:
                explicit.append((key, key_mark, value))
        explicit_by_key = {k: (km, v) for k, km, v in explicit}

        # 1) 依次展开 merge 源，收集候选（源内已按其自身 merge 合同生效）
        candidates: dict[object, list[_Candidate]] = {}
        source_count = 0
        for merge_node in merge_nodes:
            source, src_chain, src_active = self._deref(merge_node, chain, active)
            if isinstance(source, RawMapping):
                self._collect(source, candidates, source_count, "merge-map", src_chain, src_active, depth)
                source_count += 1
            elif isinstance(source, RawSequence):
                for item in source.items:
                    item_node, item_chain, item_active = self._deref(item, src_chain, src_active)
                    if not isinstance(item_node, RawMapping):
                        raise MergeTypeError(
                            f"merge 序列第 {source_count} 项不是映射（路径 {path}）"
                        )
                    self._collect(
                        item_node,
                        candidates,
                        source_count,
                        "merge-seq-item",
                        item_chain,
                        item_active,
                        depth,
                    )
                    source_count += 1
            else:
                raise MergeTypeError(
                    f"merge 键 << 的值必须是映射或映射序列，实际为 {type(source).__name__}（路径 {path}）"
                )

        # 2) 每个键的胜出者与覆盖证据
        for key, cand_list in candidates.items():
            winner = cand_list[0]  # 从左到右：第一个提供的键胜出
            evidence: list[MergeEvidence] = []
            for cand in cand_list:
                evidence.append(
                    MergeEvidence(
                        source_index=cand.source_index,
                        source_kind=cand.source_kind,
                        key_mark=cand.key_mark,
                        won=(cand is winner),
                        overridden_by=None if cand is winner else winner.key_mark,
                    )
                )
            child_path = path + (key,)
            if key in explicit_by_key:
                # 显式键恒覆盖 merge 所得，不受 << 文本位置影响
                exp_key_mark, exp_value = explicit_by_key[key]
                evidence = [
                    MergeEvidence(
                        source_index=e.source_index,
                        source_kind=e.source_kind,
                        key_mark=e.key_mark,
                        won=False,
                        overridden_by=exp_key_mark,
                    )
                    for e in evidence
                ]
                key_prov = self._prov(
                    child_path, exp_value.mark, exp_key_mark, True, chain, tuple(evidence)
                )
                child = self._mat(exp_value, child_path, chain, active, depth + 1)
            else:
                key_prov = self._prov(
                    child_path,
                    winner.value.mark,
                    winner.key_mark,
                    False,
                    winner.chain,
                    tuple(evidence),
                )
                child = self._mat(
                    winner.value, child_path, winner.chain, active, depth + 1
                )
            # 键级 prov 是该有效键的权威来源（含别名链/merge 证据），同步到子节点，
            # 使 doc.find(path).prov 与 mapping.key_prov[key] 给出同一答案
            child.prov = key_prov
            result.key_prov[key] = key_prov
            result.entries[key] = child

        # 3) 仅由显式键提供的键（无 merge 候选，证据为空）
        for key, key_mark, value in explicit:
            if key in result.entries:
                continue
            key_prov = self._prov(path + (key,), value.mark, key_mark, True, chain)
            child = self._mat(value, path + (key,), chain, active, depth + 1)
            child.prov = key_prov
            result.key_prov[key] = key_prov
            result.entries[key] = child

        return result

    def _collect(
        self,
        mapping: RawMapping,
        candidates: dict[object, list[_Candidate]],
        source_index: int,
        kind: str,
        chain: tuple[AliasEvent, ...],
        active: frozenset[str],
        depth: int,
    ) -> None:
        """收集一个 merge 源映射的全部生效条目作为候选；同名子映射不深度合并。

        源自身的显式键与源内部 merge 都通过 :meth:`_effective_pairs` 参与源级胜出；
        调用方（外层映射）的显式键不在此过滤，而由 :meth:`_mat_mapping` 统一覆盖。
        """
        for eff in self._effective_pairs(mapping, chain, active, depth + 1):
            candidates.setdefault(eff.key, []).append(
                _Candidate(eff.key_mark, eff.value, source_index, kind, eff.chain)
            )

    def _effective_pairs(
        self,
        mapping: RawMapping,
        chain: tuple[AliasEvent, ...],
        active: frozenset[str],
        depth: int,
    ) -> list[_Effective]:
        """产出映射的**生效条目**：merge 源按序补缺，本映射显式键恒覆盖。

        用于把 merge 源映射整体作为候选集；外层映射自身的显式键覆盖由
        :meth:`_mat_mapping` 统一处理，因此候选收集走 :meth:`_merge_effective`。
        """
        merged = self._merge_effective(mapping, chain, active, depth)
        chosen: dict[object, _Effective] = {e.key: e for e in merged}
        ordered = [e.key for e in merged]
        for k, km, v in mapping.entries:
            if k is MERGE_KEY:
                continue
            eff = _Effective(k, km, v, chain)
            if k in chosen:
                chosen[k] = eff  # 本映射显式键覆盖自身 merge 候选
            else:
                chosen[k] = eff
                ordered.append(k)
        return [chosen[k] for k in ordered]

    def _merge_effective(
        self,
        mapping: RawMapping,
        chain: tuple[AliasEvent, ...],
        active: frozenset[str],
        depth: int,
    ) -> list[_Effective]:
        """仅展开映射的 merge 源（源内部递归按同一合同生效），首键胜出。

        不应用 mapping 自身的显式键——显式覆盖是消费该候选集的外层映射的职责。
        """
        if depth > self.max_depth:
            raise DepthLimitError("merge 嵌套深度超限")
        chosen: dict[object, _Effective] = {}
        ordered: list[object] = []
        for _k, _km, v in mapping.entries:
            if _k is not MERGE_KEY:
                continue
            sub_node, sub_chain, sub_active = self._deref(v, chain, active)
            src_maps: list[RawMapping] = []
            if isinstance(sub_node, RawMapping):
                src_maps = [sub_node]
            elif isinstance(sub_node, RawSequence):
                for item in sub_node.items:
                    item_node, item_chain, item_active = self._deref(item, sub_chain, sub_active)
                    if not isinstance(item_node, RawMapping):
                        raise MergeTypeError("merge 序列中存在非映射项")
                    src_maps.append(item_node)
            else:
                raise MergeTypeError("merge 键 << 的值类型非法")
            for src in src_maps:
                # 源映射自身的显式键要参与源内部的胜出判定
                for eff in self._effective_pairs(src, sub_chain, sub_active, depth + 1):
                    if eff.key not in chosen:
                        chosen[eff.key] = eff
                        ordered.append(eff.key)
        return [chosen[k] for k in ordered]

    # ------------------------------------------------------------------ #
    @staticmethod
    def _prov(
        path: tuple,
        value_mark: Mark,
        key_mark: Mark,
        explicit: bool,
        chain: tuple[AliasEvent, ...],
        evidence: tuple[MergeEvidence, ...] = (),
    ) -> Provenance:
        return Provenance(
            path=tuple(path),
            value_mark=value_mark,
            key_mark=key_mark,
            explicit=explicit,
            alias_chain=tuple(chain),
            merge_evidence=tuple(evidence),
        )


def materialize_document(doc: ParsedDocument, max_depth: int = DEFAULT_MAX_DEPTH) -> MaterializedDocument:
    return Materializer(doc, max_depth).materialize()


def materialize_all(
    docs: list[ParsedDocument], max_depth: int = DEFAULT_MAX_DEPTH
) -> list[MaterializedDocument]:
    # 每文档使用全新实例：Materializer 持有解引用/深度等过程状态，不可跨文档复用
    return [Materializer(d, max_depth).materialize() for d in docs]
