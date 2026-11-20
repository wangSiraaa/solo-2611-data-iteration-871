"""merge 键合同测试：手写小真值核对有效值与覆盖证据。"""

from __future__ import annotations

import unittest

from confcat.codec import to_plain
from confcat.errors import MergeTypeError
from confcat.materialize import materialize_all
from confcat.yaml_parser import parse_text


def mat(text: str):
    return materialize_all(parse_text(text))[0]


class MergeTruthTableTest(unittest.TestCase):
    """手写真值：merge 序列从左到右首键胜出，后者只补缺。"""

    TEXT = """
m1: &m1
  a: 10
  shared: first
m2: &m2
  a: 20
  b: 20
m3: &m3
  b: 30
  c: 30
t:
  <<:
    - *m1
    - *m2
    - *m3
"""

    def test_effective_values_truth(self):
        m = mat(self.TEXT)
        effective = to_plain(m.find(("t",)))
        # m1 提供 a/shared；m2 只补 b（a 已被 m1 占位）；m3 只补 c（b 已被 m2 占位）
        self.assertEqual(
            effective,
            {"a": 10, "shared": "first", "b": 20, "c": 30},
        )

    def test_winner_and_loser_evidence(self):
        m = mat(self.TEXT)
        t = m.find(("t",))
        a_ev = t.key_prov["a"].merge_evidence
        self.assertEqual([(e.source_index, e.won) for e in a_ev], [(0, True), (1, False)])
        # 失败者记录被胜出者位置覆盖
        self.assertIsNotNone(a_ev[1].overridden_by)
        self.assertIsNone(a_ev[0].overridden_by)
        b_ev = t.key_prov["b"].merge_evidence
        self.assertEqual([(e.source_index, e.won) for e in b_ev], [(1, True), (2, False)])
        c_ev = t.key_prov["c"].merge_evidence
        self.assertEqual([(e.source_index, e.won) for e in c_ev], [(2, True)])
        # 全部 merge 来源键都不是显式键
        self.assertFalse(t.key_prov["a"].explicit)

    def test_alias_chain_on_merged_key(self):
        m = mat(self.TEXT)
        chain = [a.alias_name for a in m.find(("t", "a")).prov.alias_chain]
        self.assertEqual(chain, ["m1"])
        chain_c = [a.alias_name for a in m.find(("t", "c")).prov.alias_chain]
        self.assertEqual(chain_c, ["m3"])


class ExplicitOverrideTest(unittest.TestCase):
    def test_explicit_after_merge_wins(self):
        m = mat(
            """
b: &b
  k: base
t:
  <<: *b
  k: explicit-after
"""
        )
        self.assertEqual(m.find(("t", "k")).value, "explicit-after")
        prov = m.find(("t", "k")).prov
        self.assertTrue(prov.explicit)
        self.assertEqual(len(prov.merge_evidence), 1)
        self.assertFalse(prov.merge_evidence[0].won)
        self.assertIsNotNone(prov.merge_evidence[0].overridden_by)

    def test_explicit_before_merge_also_wins(self):
        m = mat(
            """
b: &b
  k: base
t:
  k: explicit-before
  <<: *b
"""
        )
        self.assertEqual(m.find(("t", "k")).value, "explicit-before")
        self.assertTrue(m.find(("t", "k")).prov.explicit)
        self.assertFalse(m.find(("t", "k")).prov.merge_evidence[0].won)

    def test_explicit_does_not_leak_into_sibling_key(self):
        # 外层显式键 k 不能把 merge 源里的另一个键 j 过滤掉
        m = mat(
            """
b: &b
  k: base-k
  j: base-j
t:
  k: own
  <<: *b
"""
        )
        t = to_plain(m.find(("t",)))
        self.assertEqual(t, {"k": "own", "j": "base-j"})
        self.assertFalse(m.find(("t", "j")).prov.explicit)


class NoRecursiveMergeTest(unittest.TestCase):
    def test_nested_mapping_not_deep_merged(self):
        m = mat(
            """
base: &base
  nested:
    a: 1
    b: 2
t:
  nested:
    c: 3
  <<: *base
"""
        )
        # 显式 nested 整体胜出，base.nested 的 a/b 不被深度合并
        self.assertEqual(to_plain(m.find(("t", "nested"))), {"c": 3})

    def test_merge_source_nested_whole_wins(self):
        m = mat(
            """
base: &base
  nested:
    a: 1
t:
  <<: *base
  extra: x
"""
        )
        self.assertEqual(to_plain(m.find(("t", "nested"))), {"a": 1})


class SingleMergeAndQuotedMarkerTest(unittest.TestCase):
    def test_single_mapping_merge(self):
        m = mat(
            """
b: &b
  k: v
t:
  <<: *b
  own: 1
"""
        )
        self.assertEqual(to_plain(m.find(("t",))), {"k": "v", "own": 1})
        self.assertEqual(m.find(("t", "k")).prov.merge_evidence[0].source_kind, "merge-map")

    def test_quoted_merge_marker_is_plain_key(self):
        m = mat(
            """
t:
  "<<": not-a-merge
  real: value
"""
        )
        self.assertEqual(
            to_plain(m.find(("t",))), {"<<": "not-a-merge", "real": "value"}
        )

    def test_bad_merge_type_rejected(self):
        with self.assertRaises(MergeTypeError):
            materialize_all(parse_text("t:\n  <<: 42\n"))
        with self.assertRaises(MergeTypeError):
            materialize_all(parse_text("t:\n  <<:\n    - 1\n    - 2\n"))


class NestedMergeSourceTest(unittest.TestCase):
    def test_source_internal_explicit_overrides_its_own_merge(self):
        m = mat(
            """
ib: &ib
  x: from-base
inner: &inner
  <<: *ib
  x: inner-explicit
outer:
  <<: *inner
"""
        )
        # 源 inner 的显式键在源级胜出，outer 自身没有 x 显式键
        self.assertEqual(m.find(("outer", "x")).value, "inner-explicit")
        self.assertFalse(m.find(("outer", "x")).prov.explicit)


if __name__ == "__main__":
    unittest.main()
