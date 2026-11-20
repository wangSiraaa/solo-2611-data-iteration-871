"""受限 YAML 解析器合同测试：标量类型、键类型、锚点/别名、拒绝清单。"""

from __future__ import annotations

import unittest

from confcat.errors import (
    CircularReferenceError,
    DuplicateAnchorError,
    DuplicateKeyError,
    ForwardAliasError,
    KeyTypeError,
    TagNotAllowedError,
    YamlSyntaxError,
)
from confcat.materialize import materialize_all
from confcat.yaml_parser import parse_text


def plain(text: str):
    from confcat.codec import to_plain

    return to_plain(materialize_all(parse_text(text))[0].root)


class ScalarContractTest(unittest.TestCase):
    def test_strict_scalar_typing(self):
        doc = plain(
            """
s:
  leading: 001
  yes_word: yes
  no_word: no
  on_word: on
  date: 2026-11-18
  floatish: 1.5
  negative: -7
  zero: 0
  big: 123456789
  t: true
  f: false
  n: null
  quoted_int: "42"
  quoted_bool: "true"
  plain: hello
"""
        )["s"]
        self.assertEqual(doc["leading"], "001")
        self.assertEqual(doc["yes_word"], "yes")
        self.assertEqual(doc["no_word"], "no")
        self.assertEqual(doc["on_word"], "on")
        self.assertEqual(doc["date"], "2026-11-18")
        self.assertEqual(doc["floatish"], "1.5")
        self.assertEqual(doc["negative"], -7)
        self.assertEqual(doc["zero"], 0)
        self.assertEqual(doc["big"], 123456789)
        self.assertIs(doc["t"], True)
        self.assertIs(doc["f"], False)
        self.assertIsNone(doc["n"])
        self.assertEqual(doc["quoted_int"], "42")
        self.assertEqual(doc["quoted_bool"], "true")
        self.assertEqual(doc["plain"], "hello")

    def test_integer_form(self):
        # 0 或负号可选的非零首位：前导零整数非法 -> 字符串；-0 非规范 -> 字符串
        doc = plain("a:\n  v1: 0\n  v2: -0\n  v3: 01\n  v4: -01\n  v5: 10\n")["a"]
        self.assertEqual(doc["v1"], 0)
        self.assertEqual(doc["v2"], "-0")
        self.assertEqual(doc["v3"], "01")
        self.assertEqual(doc["v4"], "-01")
        self.assertEqual(doc["v5"], 10)

    def test_quoted_strings_and_escapes(self):
        text = 'a:\n  dq: "x\\"y"\n  sq: "it\'s"\n'
        doc = plain(text)
        self.assertEqual(doc["a"]["dq"], 'x"y')
        self.assertEqual(doc["a"]["sq"], "it's")

    def test_empty_scalar_is_null(self):
        doc = plain("a:\n  x:\n  y: null\n")
        self.assertIsNone(doc["a"]["x"])
        self.assertIsNone(doc["a"]["y"])


class KeyContractTest(unittest.TestCase):
    def test_int_and_string_keys_are_distinct(self):
        doc = plain('k:\n  1: int-one\n  "1": str-one\n  2: int-two\n')["k"]
        self.assertEqual(doc[1], "int-one")
        self.assertEqual(doc["1"], "str-one")
        self.assertEqual(doc[2], "int-two")
        self.assertEqual(len(doc), 3)

    def test_duplicate_string_key_rejected(self):
        with self.assertRaises(DuplicateKeyError):
            parse_text("a: 1\na: 2\n")

    def test_duplicate_int_key_rejected_but_mixed_type_ok(self):
        with self.assertRaises(DuplicateKeyError):
            parse_text("k:\n  1: a\n  1: b\n")
        doc = plain('k:\n  1: a\n  "1": b\n')["k"]
        self.assertEqual(doc[1], "a")
        self.assertEqual(doc["1"], "b")

    def test_bool_and_null_keys_rejected(self):
        with self.assertRaises(KeyTypeError):
            parse_text("true: 1\n")
        with self.assertRaises(KeyTypeError):
            parse_text("null: 1\n")


class AnchorAliasTest(unittest.TestCase):
    def test_alias_shares_raw_substructure(self):
        text = "b: &b\n  k: v\na1: *b\na2: *b\n"
        parsed = parse_text(text)[0]
        # raw graph 中别名指向同一锚点节点（共享子结构事实）
        self.assertIs(parsed.anchors["b"], parsed.root.get("b"))

    def test_forward_alias_rejected(self):
        with self.assertRaises(ForwardAliasError):
            parse_text("a: *x\nx: &x\n  k: 1\n")

    def test_unknown_alias_rejected(self):
        with self.assertRaises(ForwardAliasError):
            parse_text("a: *nope\n")

    def test_duplicate_anchor_rejected(self):
        with self.assertRaises(DuplicateAnchorError):
            parse_text("a: &z 1\nb: &z 2\n")

    def test_self_cycle_rejected_at_materialize(self):
        with self.assertRaises(CircularReferenceError):
            materialize_all(parse_text("a: &a\n  self: *a\n"))

    def test_sequence_cycle_rejected(self):
        with self.assertRaises(CircularReferenceError):
            materialize_all(parse_text("s: &s\n  - *s\n"))

    def test_alias_chain_evidence(self):
        text = "b: &b\n  k: v\nc:\n  <<: *b\n"
        mat = materialize_all(parse_text(text))[0]
        prov = mat.find(("c", "k")).prov
        self.assertEqual([a.alias_name for a in prov.alias_chain], ["b"])
        self.assertFalse(prov.explicit)

    def test_multidoc_same_anchor_name_isolated(self):
        text = "---\nt: &t\n  v: doc0\no: *t\n---\nt: &t\n  v: doc1\no: *t\n"
        docs = materialize_all(parse_text(text))
        self.assertEqual(docs[0].find(("o", "v")).value, "doc0")
        self.assertEqual(docs[1].find(("o", "v")).value, "doc1")


class SyntaxRejectionTest(unittest.TestCase):
    def test_custom_tag_rejected(self):
        with self.assertRaises(TagNotAllowedError):
            parse_text("a: !tag 1\n")

    def test_flow_collections_rejected(self):
        with self.assertRaises(YamlSyntaxError):
            parse_text("a: [1, 2]\n")
        with self.assertRaises(YamlSyntaxError):
            parse_text("a: {x: 1}\n")

    def test_tab_indent_rejected(self):
        with self.assertRaises(YamlSyntaxError):
            parse_text("a:\n\tb: 1\n")

    def test_comment_inside_quoted_string_preserved(self):
        doc = plain('a: "hello # not comment"\n')
        self.assertEqual(doc["a"], "hello # not comment")

    def test_sequence_structures(self):
        doc = plain("a:\n  - 1\n  - two\n  - true\n")
        self.assertEqual(doc["a"], [1, "two", True])

    def test_sequence_of_mappings(self):
        doc = plain("a:\n  - x: 1\n  - x: 2\n")
        self.assertEqual(doc["a"], [{"x": 1}, {"x": 2}])


if __name__ == "__main__":
    unittest.main()
