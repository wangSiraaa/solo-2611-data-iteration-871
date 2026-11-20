"""受限 YAML 解析器（标准库实现，无第三方依赖）。

支持子集（与题面合同严格对齐）
==============================
* 块式映射、块式序列，缩进只允许空格（Tab 缩进直接报错）；
* 双引号/单引号字符串、十进制整数（``0`` 或负号可选的非零首位）、
  ``true`` / ``false`` / ``null``；
* 未加引号的其他标量一律字符串：``yes``、``001``、日期、``1.5`` 等不转型；
* 文档内锚点 ``&name``、别名 ``*name``：锚点仅当前文档作用域，
  前向别名、未知别名、同文档重名锚点均拒绝；
* 映射 merge 键 ``<<``（merge 的值语义在 :mod:`confcat.materialize` 处理）；
* 多文档 ``---`` / ``...``，行注释 ``#``（引号内 # 不算注释）。

显式拒绝：自定义 tag（``!``）、flow 集合（``[``/``{``）、折叠/字面量块标量、
重复显式映射键、非 str/int 键。

游标不变量
==========
每个 ``_parse_*`` 块入口处 ``self.i`` 指向该块第一行；正常返回时 ``self.i``
指向该块之后**第一个未消费行**（可能是 EOF 的哨兵位置）。序列项内联映射通过
``dash_indent`` 识别外层序列边界（缩进回退到 dash 级的 ``- `` 行），在边界处
停止且不消费该行，从而正确返回多个同级序列项。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from .errors import (
    DuplicateAnchorError,
    DuplicateKeyError,
    ForwardAliasError,
    KeyTypeError,
    TagNotAllowedError,
    YamlSyntaxError,
)
from .model import (
    Mark,
    ParsedDocument,
    RawAlias,
    RawMapping,
    RawNode,
    RawScalar,
    RawSequence,
)

DEFAULT_MAX_DEPTH = 50
_INT_RE = re.compile(r"^(0|-?[1-9][0-9]*)$")
_ANCHOR_NAME_RE = re.compile(r"^[A-Za-z0-9_-]+$")

# 裸 merge 键 << 的内部哨兵；引号 "<<" 是普通字符串键，不会使用该哨兵
MERGE_KEY = object()


# --------------------------------------------------------------------------- #
# 逻辑行
# --------------------------------------------------------------------------- #
@dataclass
class LogLine:
    indent: int
    text: str
    lineno: int
    raw: str


def _strip_comment(content: str) -> str:
    out: list[str] = []
    quote: str | None = None
    i = 0
    while i < len(content):
        ch = content[i]
        if quote:
            out.append(ch)
            if ch == "\\" and quote == '"' and i + 1 < len(content):
                out.append(content[i + 1])
                i += 2
                continue
            if ch == quote:
                quote = None
            i += 1
            continue
        if ch in ("'", '"'):
            quote = ch
            out.append(ch)
        elif ch == "#" and (i == 0 or content[i - 1] in " \t"):
            break
        else:
            out.append(ch)
        i += 1
    return "".join(out).rstrip()


def _logical_lines(text: str) -> list[LogLine]:
    if text.startswith("﻿"):
        text = text[1:]
    lines: list[LogLine] = []
    for lineno, raw in enumerate(text.splitlines(), start=1):
        prefix = raw[: len(raw) - len(raw.lstrip(" \t"))]
        if "\t" in prefix:
            raise YamlSyntaxError("缩进不允许使用 Tab", line=lineno)
        stripped = _strip_comment(raw)
        if not stripped.strip():
            continue
        indent = len(stripped) - len(stripped.lstrip(" "))
        lines.append(LogLine(indent=indent, text=stripped.lstrip(" "), lineno=lineno, raw=raw))
    return lines


def _split_documents(lines: list[LogLine]) -> list[list[LogLine]]:
    """按 ``---`` / ``...`` 切分文档；不在分隔符之间插入空文档。"""
    docs: list[list[LogLine]] = []
    current: list[LogLine] = []
    started = False  # 是否已遇到首个文档开始标记或正文
    for line in lines:
        if line.indent == 0 and line.text == "---":
            if started:
                docs.append(current)
            current = []
            started = True
            continue
        if line.indent == 0 and line.text == "...":
            if started:
                docs.append(current)
            current = []
            started = False  # 等待下一个 --- 显式开始新文档
            continue
        current.append(line)
        started = True
    if started or not docs:
        docs.append(current)
    return docs


# --------------------------------------------------------------------------- #
# 行内词法
# --------------------------------------------------------------------------- #
@dataclass
class Token:
    kind: str
    value: object
    quoted: bool
    column: int
    text: str


def _unescape_double(s: str, lineno: int) -> str:
    out: list[str] = []
    i = 0
    simple = {"n": "\n", "t": "\t", "r": "\r", '"': '"', "\\": "\\", "0": "\0", "b": "\b", "f": "\f"}
    while i < len(s):
        ch = s[i]
        if ch != "\\":
            out.append(ch)
            i += 1
            continue
        if i + 1 >= len(s):
            raise YamlSyntaxError("双引号字符串转义不完整", line=lineno)
        nxt = s[i + 1]
        if nxt in simple:
            out.append(simple[nxt])
            i += 2
        elif nxt == "u" and re.fullmatch(r"[0-9a-fA-F]{4}", s[i + 2 : i + 6]):
            out.append(chr(int(s[i + 2 : i + 6], 16)))
            i += 6
        else:
            raise YamlSyntaxError(f"不支持的转义序列 \\{nxt}", line=lineno)
    return "".join(out)


def _tokenize_inline(text: str, base_col: int, lineno: int) -> list[Token]:
    tokens: list[Token] = []
    i = 0
    n = len(text)
    while i < n:
        if text[i] == " ":
            i += 1
            continue
        col = base_col + i
        ch = text[i]
        if ch == "!":
            raise TagNotAllowedError("本题禁止自定义 tag", line=lineno)
        if ch in "[{":
            raise YamlSyntaxError("本子集不支持 flow 集合（[] / {}），请使用块式语法", line=lineno)
        if ch in "|>":
            raise YamlSyntaxError("本子集不支持折叠/字面量块标量（| / >）", line=lineno)
        if ch in "&*":
            m = re.match(r"[A-Za-z0-9_-]+", text[i + 1 :])
            if not m:
                raise YamlSyntaxError(f"非法锚点/别名声明 {text[i:]}", line=lineno)
            name = m.group(0)
            tokens.append(Token("anchor" if ch == "&" else "alias", name, False, col, text[i : i + 1 + len(name)]))
            i += 1 + len(name)
            continue
        if ch == "'":
            j = i + 1
            buf: list[str] = []
            while True:
                if j >= n:
                    raise YamlSyntaxError("单引号字符串未闭合", line=lineno)
                if text[j] == "'":
                    if j + 1 < n and text[j + 1] == "'":
                        buf.append("'")
                        j += 2
                        continue
                    break
                buf.append(text[j])
                j += 1
            tokens.append(Token("scalar", "".join(buf), True, col, text[i : j + 1]))
            i = j + 1
            continue
        if ch == '"':
            j = i + 1
            buf = []
            while True:
                if j >= n:
                    raise YamlSyntaxError("双引号字符串未闭合", line=lineno)
                if text[j] == "\\":
                    if j + 1 >= n:
                        raise YamlSyntaxError("双引号字符串转义不完整", line=lineno)
                    buf.append(text[j : j + 2])
                    j += 2
                    continue
                if text[j] == '"':
                    break
                buf.append(text[j])
                j += 1
            tokens.append(Token("scalar", _unescape_double("".join(buf), lineno), True, col, text[i : j + 1]))
            i = j + 1
            continue
        j = i
        while j < n and text[j] != " ":
            j += 1
        tokens.append(Token("scalar", text[i:j], False, col, text[i:j]))
        i = j
    return tokens


def _plain_value(raw: str) -> object:
    if raw == "true":
        return True
    if raw == "false":
        return False
    if raw == "null":
        return None
    if _INT_RE.match(raw):
        return int(raw)
    return raw


def _find_key_colon(text: str, lineno: int) -> int:
    quote: str | None = None
    for i, ch in enumerate(text):
        if quote:
            if ch == quote:
                quote = None
            continue
        if ch in ("'", '"'):
            quote = ch
        elif ch == ":" and (i + 1 == len(text) or text[i + 1] == " "):
            return i
    raise YamlSyntaxError("映射条目缺少键值分隔符 ': '", line=lineno)


def _has_colon(text: str) -> bool:
    try:
        _find_key_colon(text, -1)
        return True
    except YamlSyntaxError:
        return False


def _is_seq_start(text: str) -> bool:
    return text == "-" or text.startswith("- ")


# --------------------------------------------------------------------------- #
# 文档解析器
# --------------------------------------------------------------------------- #
class _DocParser:
    def __init__(self, lines: list[LogLine], doc_index: int, max_depth: int):
        self.lines = lines
        self.i = 0
        self.doc_index = doc_index
        self.anchors: dict[str, RawNode] = {}
        self.anchor_lines: dict[str, int] = {}
        self.pending_anchors: set[str] = set()
        self.max_depth = max_depth
        self._depth = 0

    def _cur(self) -> LogLine | None:
        return self.lines[self.i] if self.i < len(self.lines) else None

    def _current_lineno(self) -> int:
        return self.lines[self.i].lineno if self.i < len(self.lines) else -1

    def _register_anchor(self, name: str, node: RawNode, token: Token) -> None:
        if not _ANCHOR_NAME_RE.match(name):
            raise YamlSyntaxError(f"非法锚点名 {name}", line=self._current_lineno())
        if name in self.anchors:
            raise DuplicateAnchorError(
                f"锚点 {name!r} 在同一文档重复定义", line=self.anchor_lines[name]
            )
        node.anchor = name
        node.anchor_mark = Mark(self._current_lineno(), token.column, self.doc_index)
        self.anchors[name] = node
        self.anchor_lines[name] = self._current_lineno()
        self.pending_anchors.discard(name)

    def _make_alias(self, token: Token) -> RawAlias:
        name = str(token.value)
        if name not in self.anchors and name not in self.pending_anchors:
            raise ForwardAliasError(
                f"别名 *{name} 前向引用或锚点不存在（锚点仅限本文档且必须先定义）",
                line=self._current_lineno(),
            )
        return RawAlias(Mark(self._current_lineno(), token.column, self.doc_index), target=name)

    def parse(self) -> RawNode:
        if not self.lines:
            return RawScalar(Mark(1, 0, self.doc_index), None)
        root = self._parse_block()
        if self.i != len(self.lines):
            raise YamlSyntaxError("文档存在无法归属的缩进内容", line=self.lines[self.i].lineno)
        return root

    def _enter(self) -> None:
        # _depth 统计嵌套层数：根块为第 1 层，允许恰好 max_depth 层
        self._depth += 1
        if self._depth > self.max_depth:
            from .errors import DepthLimitError

            raise DepthLimitError(f"嵌套深度超过 {self.max_depth} 限制")

    def _parse_block(self) -> RawNode:
        """解析 self.i 起始的块，返回后 self.i 指向块后第一个未消费行。"""
        self._enter()
        try:
            line = self._cur()
            if line is None:
                return RawScalar(Mark(1, 0, self.doc_index), None)
            if _is_seq_start(line.text):
                return self._parse_sequence(line.indent)
            return self._parse_mapping(line.indent)
        finally:
            self._depth -= 1

    # -- 序列 --------------------------------------------------------------- #
    def _parse_sequence(self, indent: int) -> RawSequence:
        first = self._cur()
        seq = RawSequence(Mark(first.lineno, indent, self.doc_index))
        while True:
            line = self._cur()
            if line is None or line.indent != indent or not _is_seq_start(line.text):
                break
            body = line.text[1:].lstrip()
            item_col = indent + 1 + (len(line.text) - 1 - len(body))
            if body == "":
                # 空 dash：消费 dash 行后，下一个更深的块作为本项
                self.i += 1
                nxt = self._cur()
                if nxt is not None and nxt.indent > indent:
                    item = self._parse_block()
                else:
                    item = RawScalar(Mark(line.lineno, item_col, self.doc_index), None)
            elif _has_colon(body):
                # 项内联映射：不预推进，把 dash 行位置当作“合成键行”交给映射，
                # 由映射在循环顶部统一消费；映射遇外层 dash 边界自动停止并保留该行
                child = LogLine(item_col, body, line.lineno, line.raw)
                item = self._parse_mapping(item_col, dash_indent=indent, first_line=child)
            else:
                # 项行内标量/别名：消费 dash 行后解析
                self.i += 1
                item = self._inline_value(LogLine(item_col, body, line.lineno, line.raw), indent)
            seq.items.append(item)
        return seq

    # -- 映射 --------------------------------------------------------------- #
    def _parse_mapping(
        self,
        indent: int,
        dash_indent: int | None = None,
        first_line: LogLine | None = None,
    ) -> RawMapping:
        origin = first_line
        mapping = RawMapping(Mark((first_line or self._cur()).lineno, indent, self.doc_index))
        seen: dict[tuple[type, object], int] = {}

        def at_outer_boundary(cand: LogLine | None) -> bool:
            return (
                dash_indent is not None
                and cand is not None
                and cand.indent <= dash_indent
                and _is_seq_start(cand.text)
            )

        while True:
            line = origin if origin is not None else self._cur()
            origin = None
            if line is None or line.indent != indent or at_outer_boundary(line):
                break
            # 统一游标约定：入口 self.i 指向当前键行，此处消费后 self.i 指向下一物理行。
            # 序列项内联映射也遵守该约定（序列在调用前把游标定位到合成键行位置）。
            self.i += 1
            colon = _find_key_colon(line.text, line.lineno)
            key_text = line.text[:colon].rstrip()
            key_col = line.indent + (len(line.text[:colon]) - len(key_text))
            key_tokens = _tokenize_inline(key_text, key_col, line.lineno)
            if len(key_tokens) != 1 or key_tokens[0].kind != "scalar":
                raise YamlSyntaxError("映射键必须是单个标量，不允许锚点/别名键", line=line.lineno)
            kt = key_tokens[0]
            if not kt.quoted and kt.value == "<<":
                key_value = MERGE_KEY
            else:
                key_value = kt.value if kt.quoted else _plain_value(str(kt.value))
            if key_value is not MERGE_KEY:
                if not kt.quoted and key_value is None:
                    raise KeyTypeError("null 不能作为映射键", line=line.lineno)
                if isinstance(key_value, bool):
                    raise KeyTypeError("映射键仅允许 str / int 两类", line=line.lineno)
                if not isinstance(key_value, (str, int)):
                    raise KeyTypeError("映射键仅允许 str / int 两类", line=line.lineno)
            sig = (type(key_value), key_value)
            if sig in seen:
                raise DuplicateKeyError(
                    f"重复显式映射键 {key_value!r}（类型 {type(key_value).__name__}），"
                    f"首次出现于第 {seen[sig]} 行",
                    line=line.lineno,
                )
            seen[sig] = line.lineno
            key_mark = Mark(line.lineno, kt.column, self.doc_index)

            rest = line.text[colon + 1 :].lstrip()
            rest_col = line.indent + colon + 1 + (len(line.text[colon + 1 :]) - len(rest))
            if rest == "":
                # 值是更深的嵌套块，或 null。游标当前指向下一物理行。
                nxt = self._cur()
                if not at_outer_boundary(nxt) and nxt is not None and nxt.indent > indent:
                    value = self._parse_block()  # 子块自行消费并停在块后第一个未消费行
                else:
                    value = RawScalar(Mark(line.lineno, rest_col, self.doc_index), None)
            else:
                # 行内值不占后续物理行；游标已在循环顶部推进，无需再动
                value = self._inline_value(LogLine(rest_col, rest, line.lineno, line.raw), indent)
            mapping.entries.append((key_value, key_mark, value))
        return mapping

    # -- 行内值 ------------------------------------------------------------- #
    def _inline_value(self, line: LogLine, parent_indent: int) -> RawNode:
        tokens = _tokenize_inline(line.text, line.indent, line.lineno)
        anchors = [t for t in tokens if t.kind == "anchor"]
        aliases = [t for t in tokens if t.kind == "alias"]
        scalars = [t for t in tokens if t.kind == "scalar"]
        if len(anchors) > 1:
            raise YamlSyntaxError("一个节点最多声明一个锚点", line=line.lineno)
        if aliases:
            if len(tokens) != 1:
                raise YamlSyntaxError("别名 *name 不能与其他 token 组合", line=line.lineno)
            node: RawNode = self._make_alias(aliases[0])
            if anchors:
                self._register_anchor(str(anchors[0].value), node, anchors[0])
            return node
        if scalars:
            if len(scalars) != 1:
                raise YamlSyntaxError("行内标量格式非法", line=line.lineno)
            st = scalars[0]
            val = st.value if st.quoted else _plain_value(str(st.value))
            node = RawScalar(Mark(line.lineno, st.column, self.doc_index), value=val, quoted=st.quoted)
            if anchors:
                self._register_anchor(str(anchors[0].value), node, anchors[0])
            return node
        at = anchors[0]
        anchor_name = str(at.value)
        self.pending_anchors.add(anchor_name)
        nxt = self._cur()
        try:
            if nxt is not None and nxt.indent > parent_indent:
                node = self._parse_block()
            else:
                node = RawScalar(Mark(line.lineno, at.column, self.doc_index), None)
        finally:
            self.pending_anchors.discard(anchor_name)
        self._register_anchor(anchor_name, node, at)
        return node


def parse_text(text: str, max_depth: int = DEFAULT_MAX_DEPTH) -> list[ParsedDocument]:
    logical = _logical_lines(text)
    doc_lines = _split_documents(logical)
    docs: list[ParsedDocument] = []
    for idx, group in enumerate(doc_lines):
        parser = _DocParser(group, idx, max_depth)
        root = parser.parse()
        docs.append(ParsedDocument(index=idx, root=root, anchors=dict(parser.anchors), source_text=text))
    return docs


def parse_file(path: str | Path, max_depth: int = DEFAULT_MAX_DEPTH) -> list[ParsedDocument]:
    return parse_text(Path(path).read_text(encoding="utf-8"), max_depth)
