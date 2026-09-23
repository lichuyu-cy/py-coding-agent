"""Domain 错误层次。

只定义分类与结构化字段，不包含恢复策略（恢复策略属于 Pipeline / Runtime）。
所有错误继承 HarnessError，便于上层按类统一处理。
"""


class HarnessError(Exception):
    """项目自定义错误的基类。"""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class MessageValidationError(HarnessError):
    """消息校验失败：重复 ID、孤立工具结果、非法 ordinal、畸形结构等。

    code 为稳定字符串，供测试与结构化拒绝结果引用；不得依赖 message 文案。
    """

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class SchemaVersionError(MessageValidationError):
    """未知或不兼容的 schema 版本。拒绝加载，不做静默修复。"""

    def __init__(self, message: str) -> None:
        super().__init__("schema_version_unsupported", message)


class StateTransitionError(HarnessError):
    """非法状态转换或序号冲突。

    current_state 记录发起转换时的状态，trigger 记录被拒绝的触发事件。
    """

    def __init__(
        self,
        message: str,
        *,
        current_state: str | None = None,
        trigger: str | None = None,
    ) -> None:
        super().__init__(message)
        self.current_state = current_state
        self.trigger = trigger
