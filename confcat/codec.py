"""物化配置树的 JSON 编解码与取值辅助。

持久化时显式携带键类型标签，确保整数键 ``1`` 与字符串键 ``"1"`` 重开后仍是两个键；
全部来源证据（位置、别名链、merge 覆盖证据）随树一起落库，重开目录即可回查配置链。
"""

from __future__ import annotations

from typing import Any

from .errors import CatalogError
from .model import (
    AliasEvent,
    Mark,
    MatMapping,
    MatNode,
    MatScalar,
    MatSequence,
    MaterializedDocument,
    MergeEvidence,
    Provenance,
)


def _encode_mark(m: Mark) -> dict:
    return {"line": m.line, "column": m.column, "doc_index": m.doc_index}


def _decode_mark(d: dict) -> Mark:
    return Mark(d["line"], d["column"], d["doc_index"])


def _encode_alias_chain(chain) -> list[dict]:
    return [
        {
            "alias": e.alias_name,
            "alias_at": _encode_mark(e.alias_mark),
            "anchor_at": _encode_mark(e.anchor_mark),
        }
        for e in chain
    ]


def _decode_alias_chain(items: list) -> tuple[AliasEvent, ...]:
    return tuple(
        AliasEvent(
            alias_name=it["alias"],
            alias_mark=_decode_mark(it["alias_at"]),
            anchor_mark=_decode_mark(it["anchor_at"]),
        )
        for it in items
    )


def _encode_evidence(evidence) -> list[dict]:
    return [
        {
            "source_index": e.source_index,
            "source_kind": e.source_kind,
            "key_at": _encode_mark(e.key_mark),
            "won": e.won,
            "overridden_by": _encode_mark(e.overridden_by) if e.overridden_by else None,
        }
        for e in evidence
    ]


def _decode_evidence(items: list) -> tuple[MergeEvidence, ...]:
    return tuple(
        MergeEvidence(
            source_index=it["source_index"],
            source_kind=it["source_kind"],
            key_mark=_decode_mark(it["key_at"]),
            won=it["won"],
            overridden_by=_decode_mark(it["overridden_by"]) if it["overridden_by"] else None,
        )
        for it in items
    )


def _encode_prov(p: Provenance | None) -> dict | None:
    if p is None:
        return None
    return {
        "path": list(p.path),
        "explicit": p.explicit,
        "value_at": _encode_mark(p.value_mark),
        "key_at": _encode_mark(p.key_mark),
        "alias_chain": _encode_alias_chain(p.alias_chain),
        "merge_evidence": _encode_evidence(p.merge_evidence),
    }


def _decode_prov(d: dict | None) -> Provenance | None:
    if d is None:
        return None
    return Provenance(
        path=tuple(d["path"]),
        value_mark=_decode_mark(d["value_at"]),
        key_mark=_decode_mark(d["key_at"]),
        explicit=d["explicit"],
        alias_chain=_decode_alias_chain(d["alias_chain"]),
        merge_evidence=_decode_evidence(d["merge_evidence"]),
    )


def encode_node(node: MatNode) -> dict:
    """把物化节点编码为可直接 json.dumps 的普通结构。"""
    if isinstance(node, MatScalar):
        return {"t": "scalar", "v": node.value, "prov": _encode_prov(node.prov)}
    if isinstance(node, MatSequence):
        return {
            "t": "seq",
            "items": [encode_node(x) for x in node.items],
            "prov": _encode_prov(node.prov),
        }
    if isinstance(node, MatMapping):
        return {
            "t": "map",
            "entries": [
                {
                    # 键类型显式标签：i=10 与 s='10' 不会在重开后混淆
                    "kt": "i" if isinstance(k, int) else "s",
                    "k": k,
                    "v": encode_node(v),
                    "prov": _encode_prov(node.key_prov[k]),
                }
                for k, v in node.entries.items()
            ],
            "prov": _encode_prov(node.prov),
        }
    raise CatalogError(f"无法编码节点类型 {type(node).__name__}")


def decode_node(d: dict) -> MatNode:
    kind = d["t"]
    if kind == "scalar":
        return MatScalar(prov=_decode_prov(d["prov"]), value=d["v"])
    if kind == "seq":
        seq = MatSequence(prov=_decode_prov(d["prov"]))
        seq.items = [decode_node(x) for x in d["items"]]
        return seq
    if kind == "map":
        mapping = MatMapping(prov=_decode_prov(d["prov"]))
        for e in d["entries"]:
            key = int(e["k"]) if e["kt"] == "i" else str(e["k"])
            child = decode_node(e["v"])
            key_prov = _decode_prov(e["prov"])
            # 每键来源以键级 prov 为准（含别名链与 merge 覆盖证据）；
            # 同步到子节点，使 doc.find(path).prov 与 key_prov 给出同一答案
            child.prov = key_prov
            mapping.entries[key] = child
            mapping.key_prov[key] = key_prov
        return mapping
    raise CatalogError(f"无法解码节点类型 {kind}")


def encode_document(doc: MaterializedDocument) -> dict:
    return {
        "index": doc.index,
        "root": encode_node(doc.root),
        "anchor_names": list(doc.anchor_names),
        "source_hash": doc.source_hash,
    }


def decode_document(d: dict) -> MaterializedDocument:
    return MaterializedDocument(
        index=d["index"],
        root=decode_node(d["root"]),
        anchor_names=tuple(d["anchor_names"]),
        source_hash=d["source_hash"],
    )


# --------------------------------------------------------------------------- #
# 取值辅助
# --------------------------------------------------------------------------- #
def to_plain(node: MatNode) -> Any:
    """物化树 -> 普通 Python 值（深拷贝，调用方可自由修改）。"""
    if isinstance(node, MatScalar):
        return node.value
    if isinstance(node, MatSequence):
        return [to_plain(x) for x in node.items]
    if isinstance(node, MatMapping):
        return {k: to_plain(v) for k, v in node.entries.items()}
    raise CatalogError(f"未知节点类型 {type(node).__name__}")


def get_provenance(node: MatNode) -> Provenance | None:
    return node.prov


def path_str(path: tuple) -> str:
    return "/".join(str(p) for p in path)


def parse_path(spec: str | tuple) -> tuple:
    """把 ``a/b/2/c`` 形式的路径解析为元组；数字段不自动转型，保持字符串语义。"""
    if isinstance(spec, tuple):
        return spec
    if spec in ("", "/"):
        return ()
    return tuple(part for part in spec.strip("/").split("/") if part != "")
