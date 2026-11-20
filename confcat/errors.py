"""全部合同级错误类型。解析期错误与消费期拒绝分开，便于整次任务回滚。"""


class ConfCatError(Exception):
    """本项目所有错误的基类。"""


class YamlSyntaxError(ConfCatError):
    """词法/语法不符合本题支持的受限 YAML 子集。"""

    def __init__(self, message: str, line: int | None = None, filename: str | None = None):
        self.line = line
        self.filename = filename
        where = ""
        if filename:
            where += f"{filename}:"
        if line is not None:
            where += f"{line}:"
        super().__init__(f"{where} {message}".strip())


class DuplicateKeyError(YamlSyntaxError):
    """同一映射中出现重复显式键（字符串键相同才冲突）。"""


class DuplicateAnchorError(YamlSyntaxError):
    """同一文档内锚点重名。"""


class ForwardAliasError(YamlSyntaxError):
    """别名指向当前文档后续才定义的锚点（前向别名拒绝）。"""


class UnknownAnchorError(YamlSyntaxError):
    """别名找不到同文档内的锚点。"""


class CircularReferenceError(ConfCatError):
    """别名展开形成循环引用。"""


class TagNotAllowedError(YamlSyntaxError):
    """出现自定义 tag（本题禁止）。"""


class MergeTypeError(ConfCatError):
    """merge 键的值既不是映射也不是映射序列。"""


class KeyTypeError(YamlSyntaxError):
    """映射键不是本期允许的 str / int 两类。"""


class DepthLimitError(ConfCatError):
    """嵌套深度超过限制（防止循环/过深结构无限展开）。"""


class CatalogError(ConfCatError):
    """目录操作错误（产品不存在、文档/路径缺失等）。"""


class TaskRejected(ConfCatError):
    """消费任务被整次拒绝；携带配置路径与原因，任务不会写入任何结果。"""

    def __init__(self, message: str, path: tuple = (), reason: str = ""):
        self.path = tuple(path)
        self.reason = reason
        prefix = f"[{'/'.join(str(p) for p in self.path)}] " if self.path else ""
        super().__init__(f"{prefix}{message}")
