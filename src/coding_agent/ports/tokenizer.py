"""Tokenizer 端口：token 估算的预留接口。

- `TokenCounter` 只定义估算能力；预算决策（plan/decide）在阶段 13 由 TokenManager 实现。
- `SimpleTokenCounter` 是启发式占位实现（约 4 字符/token），阶段 13 将被真实估算取代；
  它只做预估，不代表供应商最终计费。
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Protocol

__all__ = ["SimpleTokenCounter", "TokenCounter"]


class TokenCounter(Protocol):
    """统一的文本 token 估算协议（禁止各模块自行各算一套）。"""

    def count_text(self, text: str) -> int: ...


@dataclass(frozen=True, slots=True)
class SimpleTokenCounter:
    """启发式占位计数器：≈ chars_per_token 字符一个 token。"""

    chars_per_token: float = 4.0

    def count_text(self, text: str) -> int:
        if not text:
            return 0
        return max(1, math.ceil(len(text) / self.chars_per_token))
