"""领域对象与时刻工具。

所有时刻一律折算为当日 UTC 分钟数（int），区间为左闭右开 [start, end)。
功率、额度单位均为 MW。
"""
from __future__ import annotations

from dataclasses import dataclass


def parse_hhmm(text: str) -> int:
    """把 HH:MM 解析为当日分钟数。"""
    hh, mm = text.strip().split(":")
    minutes = int(hh) * 60 + int(mm)
    if not 0 <= minutes < 24 * 60:
        raise ValueError(f"invalid HH:MM {text!r}")
    return minutes


def fmt_hhmm(minutes: int) -> str:
    """把当日分钟数格式化回 HH:MM。"""
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


def num(value: float) -> float | int:
    """JSON 输出用：整数值写成 int，其余保留 float。"""
    return int(value) if float(value).is_integer() else value


@dataclass(frozen=True)
class Assembly:
    """站点在某段时间内投产的能力组成。

    资格按"站点 + 能力组成"认定；直流扩容等组成变更投产即新的 assembly，
    不自动沿用旧组成的认证（除非认证记录 previous_eligible 明确允许）。
    """

    site: str
    assembly: str
    connection: str
    valid_from: int
    valid_to: int

    def active(self, at: int) -> bool:
        return self.valid_from <= at < self.valid_to


@dataclass(frozen=True)
class TestWindow:
    """抽检窗口通知：开始、结束、时间格、正式截止与最低达标值。"""

    test: str
    assembly: str
    start: int
    end: int
    grid_minutes: int
    evidence_deadline: int
    firm_threshold: float

    def required_grids(self) -> list[int]:
        return list(range(self.start, self.end, self.grid_minutes))


@dataclass(frozen=True)
class Measurement:
    """时段计量。test 为空表示交割实测；rev 在同一测量上递增（更正）。"""

    test: str | None
    assembly: str
    at: int
    mw: float
    received_at: int
    rev: int

    @property
    def is_delivery(self) -> bool:
        return self.test is None


@dataclass(frozen=True)
class Certification:
    """认证决定：批准容量、适用时段、是否可沿用旧能力。"""

    certificate: str
    test: str | None
    assembly: str
    start: int
    end: int
    max_mw: float
    decided_at: int
    previous_eligible: bool


@dataclass(frozen=True)
class ExportLimit:
    """接入点外送限额的一个版本；rev 递增，按 published_at 生效。"""

    connection: str
    start: int
    end: int
    mw: float
    published_at: int
    rev: int


@dataclass(frozen=True)
class Segment:
    """申请的一个时段段：各段请求量可不同。"""

    start: int
    end: int
    mw: float


@dataclass(frozen=True)
class Submission:
    """一次申报提交（一个申请可含多个时段段，全额全段才受理）。"""

    request: str
    submission: str
    assembly: str
    received_at: int
    hold_until: int
    segments: tuple[Segment, ...]


@dataclass(frozen=True)
class Receipt:
    """市场回执。decision 为 accepted 或 reduce；reduce 时 span 给出调减窗口。"""

    receipt: str
    submission: str
    at: int
    decision: str
    span: Segment | None
