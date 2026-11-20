"""从资料包（CSV）装载事件并构建台账。

装载前先经 inspect_inputs 校验资料包结构与摘要，保证输入可核验。
"""
from __future__ import annotations

import csv
import sys
from pathlib import Path

from .ledger import Ledger
from .model import (
    Assembly,
    Certification,
    ExportLimit,
    Measurement,
    Receipt,
    Segment,
    Submission,
    TestWindow,
    parse_hhmm,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from inspect_inputs import inspect_inputs  # noqa: E402

# 同一时刻到达时的处理顺序：计量 < 认证 < 限额 < 申请 < 回执
_TYPE_ORDER = {"measurement": 0, "certification": 1, "limit": 2, "request": 3, "receipt": 4}


def _rows(directory: Path, name: str) -> list[dict]:
    with (directory / f"{name}.csv").open(encoding="utf-8", newline="") as stream:
        return list(csv.DictReader(stream, strict=True))


def load_pack(directory: Path | str, validate: bool = True) -> tuple[Ledger, dict]:
    """读取资料包，按 (时刻, 类型, 原序) 回放事件，返回 (台账, 校验信息)。"""
    directory = Path(directory)
    pack_info = inspect_inputs(directory) if validate else {"input_pack": None, "tables": {}}

    assemblies = {}
    for r in _rows(directory, "assemblies"):
        asm = Assembly(
            site=r["site"],
            assembly=r["assembly"],
            connection=r["connection"],
            valid_from=parse_hhmm(r["from"]),
            valid_to=parse_hhmm(r["to"]),
        )
        assemblies[asm.assembly] = asm

    tests = {}
    for r in _rows(directory, "test_windows"):
        w = TestWindow(
            test=r["test"],
            assembly=r["assembly"],
            start=parse_hhmm(r["start"]),
            end=parse_hhmm(r["end"]),
            grid_minutes=int(r["minutes"]),
            evidence_deadline=parse_hhmm(r["evidence_deadline"]),
            firm_threshold=float(r["firm_threshold"]),
        )
        tests[w.test] = w

    ledger = Ledger(assemblies, tests)
    events = []

    for i, r in enumerate(_rows(directory, "measurements")):
        m = Measurement(
            test=r["test"] or None,
            assembly=r["assembly"],
            at=parse_hhmm(r["at"]),
            mw=float(r["mw"]),
            received_at=parse_hhmm(r["received_at"]),
            rev=int(r["rev"]),
        )
        events.append((m.received_at, _TYPE_ORDER["measurement"], i, ("measurement", m)))

    for i, r in enumerate(_rows(directory, "certifications")):
        c = Certification(
            certificate=r["certificate"],
            test=r["test"] or None,
            assembly=r["assembly"],
            start=parse_hhmm(r["start"]),
            end=parse_hhmm(r["end"]),
            max_mw=float(r["max_mw"]),
            decided_at=parse_hhmm(r["decided_at"]),
            previous_eligible=r["previous_eligible"].strip().lower() == "true",
        )
        events.append((c.decided_at, _TYPE_ORDER["certification"], i, ("certification", c)))

    for i, r in enumerate(_rows(directory, "export_limits")):
        l = ExportLimit(
            connection=r["connection"],
            start=parse_hhmm(r["start"]),
            end=parse_hhmm(r["end"]),
            mw=float(r["mw"]),
            published_at=parse_hhmm(r["published_at"]),
            rev=int(r["rev"]),
        )
        events.append((l.published_at, _TYPE_ORDER["limit"], i, ("limit", l)))

    # 一个申请可含多行（多个时段段），按 submission 归并
    submissions: dict[str, dict] = {}
    for i, r in enumerate(_rows(directory, "requests")):
        key = r["submission"]
        seg = Segment(parse_hhmm(r["start"]), parse_hhmm(r["end"]), float(r["mw"]))
        if key in submissions:
            prev = submissions[key]
            for field in ("request", "assembly", "received_at", "hold_until"):
                if prev["row"][field] != r[field]:
                    raise ValueError(f"requests: submission {key} 的 {field} 不一致")
            prev["segments"].append(seg)
        else:
            submissions[key] = {"row": r, "segments": [seg], "seq": i}
    for key, item in submissions.items():
        r = item["row"]
        sub = Submission(
            request=r["request"],
            submission=key,
            assembly=r["assembly"],
            received_at=parse_hhmm(r["received_at"]),
            hold_until=parse_hhmm(r["hold_until"]),
            segments=tuple(item["segments"]),
        )
        events.append((sub.received_at, _TYPE_ORDER["request"], item["seq"], ("request", sub)))

    for i, r in enumerate(_rows(directory, "market_receipts")):
        span = None
        if r["start"] and r["end"] and r["mw"]:
            span = Segment(parse_hhmm(r["start"]), parse_hhmm(r["end"]), float(r["mw"]))
        rc = Receipt(
            receipt=r["receipt"],
            submission=r["submission"],
            at=parse_hhmm(r["at"]),
            decision=r["decision"],
            span=span,
        )
        events.append((rc.at, _TYPE_ORDER["receipt"], i, ("receipt", rc)))

    for _, _, _, (kind, obj) in sorted(events, key=lambda e: (e[0], e[1], e[2])):
        if kind == "measurement":
            ledger.apply_measurement(obj)
        elif kind == "certification":
            ledger.apply_certification(obj)
        elif kind == "limit":
            ledger.apply_limit(obj)
        elif kind == "request":
            ledger.apply_submission(obj)
        else:
            ledger.apply_receipt(obj)

    return ledger, pack_info
