"""申报台账：按到达顺序吸收事件，推导资格、占用、受理与履约视图。

设计要点
- 任何结论都按"当时已知"重放：证据、限额、认证状态均按时刻求值。
- 暂占（hold）到 hold_until 未受理即释放；已受理份额只凭市场显式调减释放，
  不因资格复核、限额缩减或内部重试而悄悄释放。
- 暂占过期后的受理记为待核 claim：不占容量、不悄悄丢弃，持续列入风险视图。
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .model import (
    Assembly,
    Certification,
    ExportLimit,
    Measurement,
    Receipt,
    Segment,
    Submission,
    TestWindow,
    fmt_hhmm,
    num,
)

DELIVERY_GRID_MINUTES = 10  # 交割核对时间格（分钟）
EPS = 1e-9


def _overlaps(s1: int, e1: int, s2: int, e2: int) -> bool:
    return s1 < e2 and s2 < e1


@dataclass(frozen=True)
class Occupation:
    """某申请在站点资格或接入点限额上占据的容量。"""

    assembly: str
    connection: str
    start: int
    end: int
    mw: float
    kind: str  # accepted | hold
    submission: str
    created_at: int

    def covers(self, t: int) -> bool:
        return self.start <= t < self.end


@dataclass
class SubmissionState:
    submission: Submission
    status: str  # held | accepted | rejected | expired
    why: list[dict] | None = None
    rejection: dict | None = None
    accepted_at: int | None = None
    accepted_receipt: str | None = None
    duplicate_receipts: list[str] = field(default_factory=list)
    reductions: list[dict] = field(default_factory=list)
    claims: list[dict] = field(default_factory=list)


class Ledger:
    """容量申报台账。事件经 apply_* 进入，视图按任意时刻重放推导。"""

    def __init__(self, assemblies: dict[str, Assembly], tests: dict[str, TestWindow]):
        self.assemblies = dict(assemblies)
        self.tests = dict(tests)
        self.measurements: list[Measurement] = []
        self.certifications: dict[str, Certification] = {}
        self.limits: list[ExportLimit] = []
        self.submissions: dict[str, SubmissionState] = {}
        self.receipts_seen: set[str] = set()
        self.unrecognized_receipts: list[Receipt] = []
        self.events: list[dict] = []
        self.now = 0

    # ------------------------------------------------------------------ 事件

    def _log(self, at: int, kind: str, **detail) -> None:
        self.now = max(self.now, at)
        entry = {"seq": len(self.events) + 1, "at": fmt_hhmm(at), "kind": kind}
        entry.update(detail)
        self.events.append(entry)

    def _expire_holds(self, tau: int) -> None:
        for st in self.submissions.values():
            if st.status == "held" and st.submission.hold_until <= tau:
                st.status = "expired"
                self._log(
                    st.submission.hold_until,
                    "hold_expired",
                    submission=st.submission.submission,
                    hold_until=fmt_hhmm(st.submission.hold_until),
                )

    def apply_measurement(self, m: Measurement) -> None:
        self._expire_holds(m.received_at)
        self.measurements.append(m)
        kind = "delivery_measurement" if m.is_delivery else "test_measurement"
        self._log(
            m.received_at,
            kind,
            test=m.test,
            assembly=m.assembly,
            grid=fmt_hhmm(m.at),
            mw=num(m.mw),
            rev=m.rev,
        )
        if m.is_delivery:
            return
        # 计量更正若使已认定试验不再成立，引用该试验的认证挂复核
        for cert in self.certifications.values():
            if cert.test != m.test or cert.decided_at > m.received_at:
                continue
            before = self.certification_state(cert, m.received_at - 1)
            after = self.certification_state(cert, m.received_at)
            if before != after:
                self._log(
                    m.received_at,
                    "certification_state_changed",
                    certificate=cert.certificate,
                    test=cert.test,
                    previous=before,
                    current=after,
                    cause={"measurement_grid": fmt_hhmm(m.at), "rev": m.rev},
                    scope={
                        "assembly": cert.assembly,
                        "start": fmt_hhmm(cert.start),
                        "end": fmt_hhmm(cert.end),
                    },
                )

    def apply_certification(self, c: Certification) -> None:
        self._expire_holds(c.decided_at)
        self.certifications[c.certificate] = c
        self._log(
            c.decided_at,
            "certification_decided",
            certificate=c.certificate,
            test=c.test,
            assembly=c.assembly,
            max_mw=num(c.max_mw),
            state=self.certification_state(c, c.decided_at),
        )

    def apply_limit(self, limit: ExportLimit) -> None:
        self._expire_holds(limit.published_at)
        self.limits.append(limit)
        self._log(
            limit.published_at,
            "limit_published",
            connection=limit.connection,
            start=fmt_hhmm(limit.start),
            end=fmt_hhmm(limit.end),
            mw=num(limit.mw),
            rev=limit.rev,
        )

    def apply_submission(self, sub: Submission) -> None:
        self._expire_holds(sub.received_at)
        if sub.submission in self.submissions:
            # 内部重试 / 重复投递：不重复占额
            self._log(sub.received_at, "submission_duplicate", submission=sub.submission)
            return
        conflicts, snapshot = self._evaluate(sub)
        st = SubmissionState(submission=sub, status="held")
        self.submissions[sub.submission] = st
        if conflicts:
            st.status = "rejected"
            st.rejection = {
                "evaluated_at": fmt_hhmm(sub.received_at),
                "policy": "all_or_nothing",
                "conflicts": conflicts,
            }
            self._log(
                sub.received_at,
                "submission_rejected",
                submission=sub.submission,
                conflicts=len(conflicts),
            )
        else:
            st.why = snapshot
            self._log(
                sub.received_at,
                "hold_placed",
                submission=sub.submission,
                hold_until=fmt_hhmm(sub.hold_until),
                segments=len(sub.segments),
            )

    def apply_receipt(self, receipt: Receipt) -> None:
        self._expire_holds(receipt.at)
        if receipt.receipt in self.receipts_seen:
            # 同一回执重复投递：幂等，不再占一次
            st = self.submissions.get(receipt.submission)
            if st is not None:
                st.duplicate_receipts.append(receipt.receipt)
            self._log(receipt.at, "receipt_duplicate", receipt=receipt.receipt,
                      submission=receipt.submission)
            return
        self.receipts_seen.add(receipt.receipt)
        st = self.submissions.get(receipt.submission)
        if st is None:
            self.unrecognized_receipts.append(receipt)
            self._log(receipt.at, "receipt_unrecognized", receipt=receipt.receipt,
                      submission=receipt.submission)
            return
        if receipt.decision == "accepted":
            self._apply_acceptance(st, receipt)
        elif receipt.decision == "reduce":
            self._apply_reduction(st, receipt)
        else:
            self._log(receipt.at, "receipt_unknown_decision", receipt=receipt.receipt,
                      decision=receipt.decision)

    def _apply_acceptance(self, st: SubmissionState, receipt: Receipt) -> None:
        sub = st.submission
        if st.accepted_at is not None:
            # 重复受理：幂等，不再占一次
            st.duplicate_receipts.append(receipt.receipt)
            self._log(receipt.at, "acceptance_duplicate", receipt=receipt.receipt,
                      submission=sub.submission)
            return
        if st.status == "held" and receipt.at < sub.hold_until:
            st.status = "accepted"
            st.accepted_at = receipt.at
            st.accepted_receipt = receipt.receipt
            self._log(receipt.at, "acceptance_confirmed", receipt=receipt.receipt,
                      submission=sub.submission)
            return
        # 暂占已过期（或申请已被拒）后的受理：悬挂核查，不占容量、不悄悄丢弃
        risks = self._claim_risks(st, receipt)
        st.claims.append(
            {
                "receipt": receipt.receipt,
                "at": receipt.at,
                "risks": risks,
                "segments": list(sub.segments),
            }
        )
        self._log(receipt.at, "acceptance_pending_review", receipt=receipt.receipt,
                  submission=sub.submission, risks=[r["kind"] for r in risks])

    def _apply_reduction(self, st: SubmissionState, receipt: Receipt) -> None:
        if st.accepted_at is None or receipt.span is None:
            self._log(receipt.at, "reduction_inconsistent", receipt=receipt.receipt,
                      submission=st.submission.submission,
                      reason="submission_not_accepted_or_span_missing")
            return
        span = receipt.span
        pieces = self.pieces_as_of(st, receipt.at)
        for p in pieces:
            if _overlaps(p.start, p.end, span.start, span.end) and span.mw > p.mw + EPS:
                self._log(receipt.at, "reduction_inconsistent", receipt=receipt.receipt,
                          submission=st.submission.submission,
                          reason="reduce_mw_exceeds_current_obligation")
                return
        before = [p for p in pieces if _overlaps(p.start, p.end, span.start, span.end)]
        freed = [
            Segment(max(p.start, span.start), min(p.end, span.end), p.mw - min(span.mw, p.mw))
            for p in before
        ]
        freed = [f for f in freed if f.mw > EPS]
        st.reductions.append(
            {"receipt": receipt.receipt, "at": receipt.at, "span": span, "freed": freed}
        )
        self._log(
            receipt.at,
            "reduction_applied",
            receipt=receipt.receipt,
            submission=st.submission.submission,
            span={"start": fmt_hhmm(span.start), "end": fmt_hhmm(span.end)},
            mw=num(span.mw),
            freed_mw=num(sum(f.mw for f in freed)),
        )

    # ------------------------------------------------------------------ 证据与认证

    def evidence_state(self, test_id: str, tau: int) -> dict:
        """某试验在 tau 时刻的证据状态。

        必需格最早版本须在正式截止前到达才算建立；已建立格的更正（更高 rev）
        始终生效。缺任何必需格：incomplete —— 既不达标也不是零出力。
        """
        window = self.tests[test_id]
        grids = []
        for g in window.required_grids():
            revs = sorted(
                (
                    m
                    for m in self.measurements
                    if m.test == test_id
                    and m.assembly == window.assembly
                    and m.at == g
                    and m.received_at <= tau
                ),
                key=lambda m: (m.rev, m.received_at),
            )
            established = bool(revs) and revs[0].received_at <= window.evidence_deadline
            effective = revs[-1] if established else None
            grids.append(
                {
                    "at": g,
                    "established": established,
                    "revisions": revs,
                    "effective": effective,
                }
            )
        if any(not g["established"] for g in grids):
            result = "incomplete"
        elif all(g["effective"].mw + EPS >= window.firm_threshold for g in grids):
            result = "pass"
        else:
            result = "fail"
        return {"test": test_id, "at_time": tau, "grids": grids, "result": result}

    def certification_state(self, cert: Certification, tau: int) -> str:
        """usable | under_review | unsupported。

        under_review：决定时证据达标，事后计量更正使其不再成立（挂复核）。
        unsupported：决定时证据即不完整或不达标（含试验未知/组成不符）。
        """
        if cert.test is None:
            return "usable"
        window = self.tests.get(cert.test)
        if window is None or window.assembly != cert.assembly:
            return "unsupported"
        if self.evidence_state(cert.test, tau)["result"] == "pass":
            return "usable"
        then = self.evidence_state(cert.test, cert.decided_at)["result"]
        return "under_review" if then == "pass" else "unsupported"

    def qualification(self, assembly: str, t: int, tau: int, _seen: tuple = ()):
        """(capacity, usable_cert_ids, review_cert_ids)：assembly 在 t 时刻的资格容量。

        仅 usable 认证提供容量（取 max）；previous_eligible 的认证允许沿用
        同站点前一组成的能力。under_review 认证覆盖的范围禁止新占额。
        """
        if assembly in _seen:
            return 0.0, [], []
        usable, review = [], []
        for c in self.certifications.values():
            if c.assembly != assembly or c.decided_at > tau or not (c.start <= t < c.end):
                continue
            state = self.certification_state(c, tau)
            if state == "usable":
                usable.append(c)
            elif state == "under_review":
                review.append(c)
        cap = max((c.max_mw for c in usable), default=0.0)
        ids = [c.certificate for c in usable]
        review_ids = [c.certificate for c in review]
        if any(c.previous_eligible for c in usable):
            pred = self.predecessor(self.assemblies[assembly])
            if pred is not None:
                pcap, pids, prev = self.qualification(
                    pred.assembly, t, tau, _seen + (assembly,)
                )
                cap = max(cap, pcap)
                ids += pids
                review_ids += prev
        return cap, ids, review_ids

    def predecessor(self, asm: Assembly) -> Assembly | None:
        for a in self.assemblies.values():
            if (
                a.site == asm.site
                and a.assembly != asm.assembly
                and a.valid_to == asm.valid_from
            ):
                return a
        return None

    def connection_limit(self, connection: str, t: int, tau: int):
        """(mw, ExportLimit|None)：tau 时刻已知、覆盖 t 的最新限额版本。"""
        cands = [
            l
            for l in self.limits
            if l.connection == connection and l.start <= t < l.end and l.published_at <= tau
        ]
        if not cands:
            return None, None
        top = max(cands, key=lambda l: (l.rev, l.published_at))
        return top.mw, top

    # ------------------------------------------------------------------ 占用

    def status_as_of(self, st: SubmissionState, tau: int) -> str:
        if st.status == "rejected":
            return "rejected"
        if st.accepted_at is not None and st.accepted_at <= tau:
            return "accepted"
        if tau < st.submission.hold_until:
            return "held"
        return "expired"

    def pieces_as_of(self, st: SubmissionState, tau: int) -> list[Segment]:
        """已受理义务在 tau 时刻的有效分段（应用截至 tau 的调减）。"""
        pieces = [Segment(s.start, s.end, s.mw) for s in st.submission.segments]
        for red in sorted(st.reductions, key=lambda r: r["at"]):
            if red["at"] > tau:
                continue
            span = red["span"]
            nxt = []
            for p in pieces:
                if not _overlaps(p.start, p.end, span.start, span.end):
                    nxt.append(p)
                    continue
                if p.start < span.start:
                    nxt.append(Segment(p.start, span.start, p.mw))
                nxt.append(
                    Segment(max(p.start, span.start), min(p.end, span.end), min(span.mw, p.mw))
                )
                if span.end < p.end:
                    nxt.append(Segment(span.end, p.end, p.mw))
            pieces = nxt
        return [p for p in pieces if p.mw > EPS and p.start < p.end]

    def occupations(self, tau: int) -> list[Occupation]:
        """tau 时刻的确定占用：已受理（调减后）+ 未过期暂占。待核 claim 不占。"""
        occs = []
        for st in self.submissions.values():
            asm = self.assemblies[st.submission.assembly]
            status = self.status_as_of(st, tau)
            if status == "accepted":
                for p in self.pieces_as_of(st, tau):
                    occs.append(
                        Occupation(
                            asm.assembly, asm.connection, p.start, p.end, p.mw,
                            "accepted", st.submission.submission, st.accepted_at,
                        )
                    )
            elif status == "held":
                for s in st.submission.segments:
                    occs.append(
                        Occupation(
                            asm.assembly, asm.connection, s.start, s.end, s.mw,
                            "hold", st.submission.submission, st.submission.received_at,
                        )
                    )
        return occs

    # ------------------------------------------------------------------ 申请评估

    def _evaluate(self, sub: Submission):
        """全额全段评估：任一窗口冲突即整单拒绝，并列出冲突窗口与占用来源。"""
        asm = self.assemblies[sub.assembly]
        tau = sub.received_at
        occs = list(self.occupations(tau))
        conflicts: list[dict] = []
        snapshot: list[dict] = []
        for seg in sub.segments:
            points = {seg.start, seg.end, asm.valid_from, asm.valid_to}
            for o in occs:
                points.add(o.start)
                points.add(o.end)
            for c in self.certifications.values():
                if c.assembly == asm.assembly and c.decided_at <= tau:
                    points.add(c.start)
                    points.add(c.end)
            for l in self.limits:
                if l.connection == asm.connection and l.published_at <= tau:
                    points.add(l.start)
                    points.add(l.end)
            bounds = sorted(p for p in points if seg.start < p < seg.end)
            edges = [seg.start, *bounds, seg.end]
            for a, b in zip(edges, edges[1:]):
                conflicts.extend(self._window_conflicts(asm, sub, seg, a, b, occs, tau))
                snapshot.append(self._window_snapshot(asm, seg, a, b, occs, tau))
            # 同一次提交的各段在同一时刻叠加占用
            occs.append(
                Occupation(asm.assembly, asm.connection, seg.start, seg.end, seg.mw,
                           "hold", sub.submission, tau)
            )
        return self._merge_conflicts(conflicts), snapshot

    def _window_conflicts(self, asm, sub, seg, a, b, occs, tau) -> list[dict]:
        out = []
        window = {"start": fmt_hhmm(a), "end": fmt_hhmm(b)}
        if not asm.active(a):
            out.append(
                {
                    "segment": self._seg_json(seg),
                    "window": window,
                    "constraint": "assembly_inactive",
                    "detail": {
                        "assembly": asm.assembly,
                        "valid": [fmt_hhmm(asm.valid_from), fmt_hhmm(asm.valid_to)],
                    },
                }
            )
            return out
        qcap, qcerts, qreview = self.qualification(asm.assembly, a, tau)
        if qreview:
            out.append(
                {
                    "segment": self._seg_json(seg),
                    "window": window,
                    "constraint": "qualification_under_review",
                    "requested_mw": num(seg.mw),
                    "detail": {"certificates": qreview},
                }
            )
        else:
            qused = sum(o.mw for o in occs if o.assembly == asm.assembly and o.covers(a))
            if qused + seg.mw > qcap + EPS:
                out.append(
                    {
                        "segment": self._seg_json(seg),
                        "window": window,
                        "constraint": "qualification",
                        "capacity_mw": num(qcap),
                        "occupied_mw": num(qused),
                        "requested_mw": num(seg.mw),
                        "deficit_mw": num(qused + seg.mw - qcap),
                        "certificates": qcerts,
                        "tests": self._assembly_test_states(asm.assembly, tau),
                        "sources": self._sources(occs, asm.assembly, a, scope="assembly"),
                    }
                )
        lcap, lrev = self.connection_limit(asm.connection, a, tau)
        lused = sum(o.mw for o in occs if o.connection == asm.connection and o.covers(a))
        if lrev is None:
            out.append(
                {
                    "segment": self._seg_json(seg),
                    "window": window,
                    "constraint": "connection_limit",
                    "requested_mw": num(seg.mw),
                    "detail": {"connection": asm.connection, "reason": "no_published_limit"},
                }
            )
        elif lused + seg.mw > lcap + EPS:
            out.append(
                {
                    "segment": self._seg_json(seg),
                    "window": window,
                    "constraint": "connection_limit",
                    "capacity_mw": num(lcap),
                    "limit_rev": lrev.rev,
                    "occupied_mw": num(lused),
                    "requested_mw": num(seg.mw),
                    "deficit_mw": num(lused + seg.mw - lcap),
                    "sources": self._sources(occs, asm.connection, a, scope="connection"),
                }
            )
        return out

    def _window_snapshot(self, asm, seg, a, b, occs, tau) -> dict:
        qcap, qcerts, qreview = self.qualification(asm.assembly, a, tau)
        lcap, lrev = self.connection_limit(asm.connection, a, tau)
        qused = sum(o.mw for o in occs if o.assembly == asm.assembly and o.covers(a))
        lused = sum(o.mw for o in occs if o.connection == asm.connection and o.covers(a))
        return {
            "window": {"start": fmt_hhmm(a), "end": fmt_hhmm(b)},
            "requested_mw": num(seg.mw),
            "qualification": {
                "capacity_mw": num(qcap),
                "certificates": qcerts,
                "under_review": qreview,
                "occupied_mw": num(qused),
            },
            "connection": {
                "connection": asm.connection,
                "limit_mw": num(lcap) if lcap is not None else None,
                "limit_rev": lrev.rev if lrev else None,
                "occupied_mw": num(lused),
            },
        }

    def _assembly_test_states(self, assembly: str, tau: int) -> dict:
        return {
            t: self.evidence_state(t, tau)["result"]
            for t, w in self.tests.items()
            if w.assembly == assembly
        }

    @staticmethod
    def _sources(occs, key, t, scope) -> list[dict]:
        out = []
        for o in occs:
            if o.submission is None:
                continue
            if scope == "assembly" and o.assembly != key:
                continue
            if scope == "connection" and o.connection != key:
                continue
            if o.covers(t):
                out.append(
                    {
                        "submission": o.submission,
                        "kind": o.kind,
                        "mw": num(o.mw),
                        "window": [fmt_hhmm(o.start), fmt_hhmm(o.end)],
                    }
                )
        return out

    @staticmethod
    def _seg_json(seg: Segment) -> dict:
        return {"start": fmt_hhmm(seg.start), "end": fmt_hhmm(seg.end), "mw": num(seg.mw)}

    @staticmethod
    def _merge_conflicts(conflicts: list[dict]) -> list[dict]:
        """相邻窗口、成因完全一致的冲突合并为最大连续窗口。"""
        merged: list[dict] = []
        for c in conflicts:
            key = {k: v for k, v in c.items() if k != "window"}
            if (
                merged
                and merged[-1]["window"]["end"] == c["window"]["start"]
                and {k: v for k, v in merged[-1].items() if k != "window"} == key
            ):
                merged[-1]["window"]["end"] = c["window"]["end"]
            else:
                merged.append(dict(c))
        return merged

    # ------------------------------------------------------------------ 待核 claim

    def _claim_risks(self, st: SubmissionState, receipt: Receipt) -> list[dict]:
        sub = st.submission
        asm = self.assemblies[sub.assembly]
        risks: list[dict] = []
        if st.status == "rejected":
            risks.append({"kind": "submission_rejected", "detail": "申请已被全额全段拒绝"})
        elif receipt.at >= sub.hold_until:
            risks.append(
                {
                    "kind": "hold_expired",
                    "hold_until": fmt_hhmm(sub.hold_until),
                    "accepted_at": fmt_hhmm(receipt.at),
                    "late_minutes": receipt.at - sub.hold_until,
                }
            )
        # 暂占释放后容量是否已被他人占用
        for o in self.occupations(receipt.at):
            if o.created_at > sub.hold_until and any(
                _overlaps(o.start, o.end, s.start, s.end) for s in sub.segments
            ):
                risks.append(
                    {
                        "kind": "capacity_reallocated",
                        "source": o.submission,
                        "window": [fmt_hhmm(o.start), fmt_hhmm(o.end)],
                        "mw": num(o.mw),
                    }
                )
        # 以当前资格与限额复核各段是否仍放得下
        for seg in sub.segments:
            for a, b in self._elementary_for(sub, seg, receipt.at):
                qcap, _, qreview = self.qualification(asm.assembly, a, receipt.at)
                qused = sum(
                    o.mw for o in self.occupations(receipt.at)
                    if o.assembly == asm.assembly and o.covers(a)
                )
                if qreview:
                    risks.append(
                        {
                            "kind": "qualification_under_review",
                            "window": [fmt_hhmm(a), fmt_hhmm(b)],
                            "certificates": qreview,
                        }
                    )
                elif qused + seg.mw > qcap + EPS:
                    risks.append(
                        {
                            "kind": "insufficient_qualification",
                            "window": [fmt_hhmm(a), fmt_hhmm(b)],
                            "capacity_mw": num(qcap),
                            "occupied_mw": num(qused),
                            "claimed_mw": num(seg.mw),
                        }
                    )
                lcap, lrev = self.connection_limit(asm.connection, a, receipt.at)
                lused = sum(
                    o.mw for o in self.occupations(receipt.at)
                    if o.connection == asm.connection and o.covers(a)
                )
                if lrev is None or lused + seg.mw > (lcap or 0) + EPS:
                    risks.append(
                        {
                            "kind": "insufficient_connection_limit",
                            "window": [fmt_hhmm(a), fmt_hhmm(b)],
                            "capacity_mw": num(lcap) if lcap is not None else None,
                            "occupied_mw": num(lused),
                            "claimed_mw": num(seg.mw),
                        }
                    )
        return risks

    def _elementary_for(self, sub, seg, tau):
        asm = self.assemblies[sub.assembly]
        points = {seg.start, seg.end}
        for o in self.occupations(tau):
            points.add(o.start)
            points.add(o.end)
        for l in self.limits:
            if l.connection == asm.connection and l.published_at <= tau:
                points.add(l.start)
                points.add(l.end)
        bounds = sorted(p for p in points if seg.start < p < seg.end)
        edges = [seg.start, *bounds, seg.end]
        return list(zip(edges, edges[1:]))

    # ------------------------------------------------------------------ 回看视图

    def why(self, submission: str) -> dict | None:
        """当时为何能申报（或为何被拒）：评估快照 + 受理/拒绝/调减沿革。"""
        st = self.submissions.get(submission)
        if st is None:
            return None
        sub = st.submission
        out = {
            "submission": sub.submission,
            "request": sub.request,
            "assembly": sub.assembly,
            "received_at": fmt_hhmm(sub.received_at),
            "hold_until": fmt_hhmm(sub.hold_until),
            "status": self.status_as_of(st, self.now),
            "segments": [self._seg_json(s) for s in sub.segments],
        }
        if st.why is not None:
            out["evaluation"] = st.why
            out["evidence"] = self._evidence_snapshot(sub, sub.received_at)
        if st.rejection is not None:
            out["rejection"] = st.rejection
        if st.accepted_at is not None:
            out["acceptance"] = {
                "receipt": st.accepted_receipt,
                "at": fmt_hhmm(st.accepted_at),
                "duplicate_receipts": st.duplicate_receipts,
            }
        if st.reductions:
            out["reductions"] = [
                {
                    "receipt": r["receipt"],
                    "at": fmt_hhmm(r["at"]),
                    "span": self._seg_json(r["span"]),
                    "freed": [self._seg_json(f) for f in r["freed"]],
                }
                for r in st.reductions
            ]
        if st.claims:
            out["pending_claims"] = [
                {
                    "receipt": c["receipt"],
                    "at": fmt_hhmm(c["at"]),
                    "risks": c["risks"],
                }
                for c in st.claims
            ]
        return out

    def _evidence_snapshot(self, sub: Submission, tau: int) -> dict:
        snap = {}
        for t, w in self.tests.items():
            if w.assembly != sub.assembly:
                continue
            state = self.evidence_state(t, tau)
            snap[t] = {
                "result": state["result"],
                "grids": [
                    {
                        "at": fmt_hhmm(g["at"]),
                        "established": g["established"],
                        "mw": num(g["effective"].mw) if g["effective"] else None,
                        "rev": g["effective"].rev if g["effective"] else None,
                    }
                    for g in state["grids"]
                ],
            }
        return snap

    def availability(self, tau: int) -> list[dict]:
        """目前还有多少能申报：逐组成、逐窗口的资格/限额余量与阻塞原因。"""
        rows = []
        occs = self.occupations(tau)
        for asm in sorted(self.assemblies.values(), key=lambda a: a.assembly):
            points = {asm.valid_from, asm.valid_to}
            for c in self.certifications.values():
                if c.assembly == asm.assembly and c.decided_at <= tau:
                    points.add(c.start)
                    points.add(c.end)
            for l in self.limits:
                if l.connection == asm.connection and l.published_at <= tau:
                    points.add(l.start)
                    points.add(l.end)
            for o in occs:
                if o.assembly == asm.assembly or o.connection == asm.connection:
                    points.add(o.start)
                    points.add(o.end)
            bounds = sorted(points)
            for a, b in zip(bounds, bounds[1:]):
                if not asm.active(a):
                    continue
                qcap, qcerts, qreview = self.qualification(asm.assembly, a, tau)
                lcap, lrev = self.connection_limit(asm.connection, a, tau)
                if qcap <= 0 and not qreview and lrev is None:
                    continue
                qused = sum(o.mw for o in occs if o.assembly == asm.assembly and o.covers(a))
                lused = sum(
                    o.mw for o in occs if o.connection == asm.connection and o.covers(a)
                )
                if qreview:
                    blocked = "qualification_under_review"
                elif qcap <= 0:
                    blocked = "no_qualification"
                elif lrev is None:
                    blocked = "no_published_limit"
                else:
                    blocked = None
                available = (
                    0.0 if blocked else max(0.0, min(qcap - qused, (lcap or 0) - lused))
                )
                rows.append(
                    {
                        "assembly": asm.assembly,
                        "connection": asm.connection,
                        "window": [fmt_hhmm(a), fmt_hhmm(b)],
                        "qualification": {
                            "capacity_mw": num(qcap),
                            "certificates": qcerts,
                            "under_review": qreview,
                            "occupied_mw": num(qused),
                        },
                        "limit": {
                            "mw": num(lcap) if lcap is not None else None,
                            "rev": lrev.rev if lrev else None,
                            "occupied_mw": num(lused),
                        },
                        "available_mw": num(available),
                        "blocked": blocked,
                        "tests": self._assembly_test_states(asm.assembly, tau),
                        "pending_claims": self._claims_touching(asm, a, b),
                    }
                )
        return rows

    def _claims_touching(self, asm: Assembly, a: int, b: int) -> list[dict]:
        out = []
        for st in self.submissions.values():
            if not st.claims or st.submission.assembly != asm.assembly:
                continue
            for c in st.claims:
                mw = sum(
                    s.mw for s in c["segments"] if _overlaps(s.start, s.end, a, b)
                )
                if mw > 0:
                    out.append(
                        {
                            "submission": st.submission.submission,
                            "receipt": c["receipt"],
                            "claimed_mw": num(mw),
                        }
                    )
        return out

    def stock_risks(self, tau: int) -> list[dict]:
        """存量风险：确定占用越限、待核 claim 放不下、已受理量靠复核中资格支撑。"""
        risks = []
        occs = self.occupations(tau)
        # 1) 确定占用 vs 当前限额 / 资格
        points = set()
        for o in occs:
            points.add(o.start)
            points.add(o.end)
        for l in self.limits:
            if l.published_at <= tau:
                points.add(l.start)
                points.add(l.end)
        for a, b in zip(sorted(points), sorted(points)[1:]):
            for asm in self.assemblies.values():
                if not asm.active(a):
                    continue
                qcap, _, qreview = self.qualification(asm.assembly, a, tau)
                qused = sum(
                    o.mw for o in occs if o.assembly == asm.assembly and o.covers(a)
                )
                if qcap > 0 and qused > qcap + EPS:
                    risks.append(
                        {
                            "kind": "qualification_overcommitted",
                            "assembly": asm.assembly,
                            "window": [fmt_hhmm(a), fmt_hhmm(b)],
                            "capacity_mw": num(qcap),
                            "occupied_mw": num(qused),
                        }
                    )
                lcap, lrev = self.connection_limit(asm.connection, a, tau)
                lused = sum(
                    o.mw for o in occs if o.connection == asm.connection and o.covers(a)
                )
                if lrev is not None and lused > lcap + EPS:
                    risks.append(
                        {
                            "kind": "connection_overcommitted",
                            "connection": asm.connection,
                            "window": [fmt_hhmm(a), fmt_hhmm(b)],
                            "limit_mw": num(lcap),
                            "limit_rev": lrev.rev,
                            "occupied_mw": num(lused),
                            "sources": self._sources(occs, asm.connection, a, "connection"),
                        }
                    )
        # 2) 待核 claim 按当前资格/限额是否放得下
        for st in self.submissions.values():
            asm = self.assemblies[st.submission.assembly]
            for claim in st.claims:
                for seg in claim["segments"]:
                    for a, b in self._elementary_for(st.submission, seg, tau):
                        qcap, _, qreview = self.qualification(asm.assembly, a, tau)
                        qused = sum(
                            o.mw for o in occs if o.assembly == asm.assembly and o.covers(a)
                        )
                        lcap, lrev = self.connection_limit(asm.connection, a, tau)
                        lused = sum(
                            o.mw
                            for o in occs
                            if o.connection == asm.connection and o.covers(a)
                        )
                        problems = []
                        if qreview:
                            problems.append("qualification_under_review")
                        if qused + seg.mw > qcap + EPS:
                            problems.append("qualification_exceeded")
                        if lrev is None or lused + seg.mw > (lcap or 0) + EPS:
                            problems.append("connection_limit_exceeded")
                        if problems:
                            risks.append(
                                {
                                    "kind": "pending_claim_misfit",
                                    "submission": st.submission.submission,
                                    "receipt": claim["receipt"],
                                    "window": [fmt_hhmm(a), fmt_hhmm(b)],
                                    "claimed_mw": num(seg.mw),
                                    "qualification": {
                                        "capacity_mw": num(qcap),
                                        "occupied_mw": num(qused),
                                    },
                                    "limit": {
                                        "mw": num(lcap) if lcap is not None else None,
                                        "occupied_mw": num(lused),
                                    },
                                    "problems": problems,
                                }
                            )
        # 3) 已受理量由复核中的资格支撑
        for st in self.submissions.values():
            if self.status_as_of(st, tau) != "accepted":
                continue
            asm = self.assemblies[st.submission.assembly]
            for p in self.pieces_as_of(st, tau):
                mid = p.start
                _, _, qreview = self.qualification(asm.assembly, mid, tau)
                if qreview:
                    risks.append(
                        {
                            "kind": "accepted_backed_by_review",
                            "submission": st.submission.submission,
                            "certificates": qreview,
                            "window": [fmt_hhmm(p.start), fmt_hhmm(p.end)],
                            "mw": num(p.mw),
                        }
                    )
        return risks

    def delivery_watchlist(self, tau: int) -> list[dict]:
        """交割时要单独对照实测的申请及原因。"""
        actuals = self._delivery_actuals(tau)
        watch = []
        for st in self.submissions.values():
            status = self.status_as_of(st, tau)
            if status not in ("accepted",) and not st.claims:
                continue
            sub = st.submission
            reasons = []
            asm = self.assemblies[sub.assembly]
            review_certs = set()
            for seg in sub.segments:
                _, _, qreview = self.qualification(asm.assembly, seg.start, tau)
                review_certs.update(qreview)
            if review_certs:
                reasons.append(
                    {"kind": "qualification_under_review", "certificates": sorted(review_certs)}
                )
            if st.claims:
                reasons.append(
                    {
                        "kind": "acceptance_pending_review",
                        "receipts": [c["receipt"] for c in st.claims],
                    }
                )
            if st.reductions:
                reasons.append(
                    {
                        "kind": "obligation_reduced",
                        "receipts": [r["receipt"] for r in st.reductions],
                    }
                )
            missing = []
            for seg in sub.segments:
                for g in range(seg.start, seg.end, DELIVERY_GRID_MINUTES):
                    if (sub.assembly, g) not in actuals:
                        missing.append(fmt_hhmm(g))
            if missing and status == "accepted":
                reasons.append({"kind": "missing_delivery_evidence", "grids": missing})
            shared = self._shared_actuals(st, tau)
            if shared:
                reasons.append({"kind": "shared_delivery_actuals", "with": shared})
            if reasons:
                watch.append(
                    {
                        "submission": sub.submission,
                        "assembly": sub.assembly,
                        "status": status if status == "accepted" else "pending_review",
                        "reasons": reasons,
                    }
                )
        return watch

    def _shared_actuals(self, st: SubmissionState, tau: int) -> list[str]:
        mine = st.submission
        others = []
        for other in self.submissions.values():
            if other is st:
                continue
            if other.submission.assembly != mine.assembly:
                continue
            if self.status_as_of(other, tau) != "accepted" and not other.claims:
                continue
            if any(
                _overlaps(a.start, a.end, b.start, b.end)
                for a in mine.segments
                for b in other.submission.segments
            ):
                others.append(other.submission.submission)
        return sorted(others)

    def _delivery_actuals(self, tau: int) -> dict:
        actuals = {}
        for m in self.measurements:
            if m.is_delivery and m.received_at <= tau:
                key = (m.assembly, m.at)
                cur = actuals.get(key)
                if cur is None or (m.rev, m.received_at) > (cur.rev, cur.received_at):
                    actuals[key] = m
        return actuals

    def reconciliation(self, tau: int) -> list[dict]:
        """交割核对：逐申请逐段逐格区分缺证、偏差与已调减的有效义务。"""
        actuals = self._delivery_actuals(tau)
        rows = []
        for st in self.submissions.values():
            status = self.status_as_of(st, tau)
            if status == "accepted":
                basis = "accepted"
                eff_pieces = self.pieces_as_of(st, tau)
            elif st.claims:
                basis = "pending_review"
                eff_pieces = list(st.submission.segments)
            else:
                continue
            sub = st.submission
            for seg in sub.segments:
                for g in range(seg.start, seg.end, DELIVERY_GRID_MINUTES):
                    piece = next(
                        (p for p in eff_pieces if p.start <= g < p.end), None
                    )
                    effective = piece.mw if piece else 0.0
                    reds = [
                        r["receipt"]
                        for r in st.reductions
                        if r["at"] <= tau and r["span"].start <= g < r["span"].end
                    ]
                    actual = actuals.get((sub.assembly, g))
                    row = {
                        "submission": sub.submission,
                        "assembly": sub.assembly,
                        "basis": basis,
                        "grid": fmt_hhmm(g),
                        "obligation_original_mw": num(seg.mw),
                        "obligation_effective_mw": num(effective),
                        "reductions_applied": reds,
                        "shared_with": self._shared_actuals(st, tau),
                    }
                    if actual is None:
                        row["result"] = "missing_evidence"
                        row["actual_mw"] = None
                    else:
                        row["actual_mw"] = num(actual.mw)
                        row["measurement"] = {
                            "rev": actual.rev,
                            "received_at": fmt_hhmm(actual.received_at),
                        }
                        if actual.mw + EPS < effective:
                            row["result"] = "shortfall"
                            row["deviation_mw"] = num(effective - actual.mw)
                        else:
                            row["result"] = "fulfilled"
                            row["surplus_mw"] = num(actual.mw - effective)
                    rows.append(row)
        return rows

    def correction_impacts(self, tau: int) -> list[dict]:
        """从计量勘误到可用额度、存量风险与履约事实的传导链。"""
        impacts = []
        for test_id, window in sorted(self.tests.items()):
            corrections = []
            for g in window.required_grids():
                revs = [
                    m
                    for m in self.measurements
                    if m.test == test_id and m.assembly == window.assembly and m.at == g
                ]
                timely = [m for m in revs if m.received_at <= window.evidence_deadline]
                late = [m for m in revs if m.received_at > window.evidence_deadline]
                if not timely or not late:
                    continue
                base = max(timely, key=lambda m: (m.rev, m.received_at))
                latest = max(revs, key=lambda m: (m.rev, m.received_at))
                if latest.mw != base.mw:
                    corrections.append(
                        {
                            "grid": fmt_hhmm(g),
                            "from": {"mw": num(base.mw), "rev": base.rev},
                            "to": {"mw": num(latest.mw), "rev": latest.rev},
                            "received_at": fmt_hhmm(latest.received_at),
                            "evidence_deadline": fmt_hhmm(window.evidence_deadline),
                        }
                    )
            if not corrections:
                continue
            for cert in sorted(
                (c for c in self.certifications.values() if c.test == test_id),
                key=lambda c: c.certificate,
            ):
                state = self.certification_state(cert, tau)
                affected = []
                for st in self.submissions.values():
                    if self.status_as_of(st, tau) != "accepted":
                        continue
                    if st.submission.assembly != cert.assembly:
                        continue
                    for p in self.pieces_as_of(st, tau):
                        if _overlaps(p.start, p.end, cert.start, cert.end):
                            affected.append(
                                {
                                    "submission": st.submission.submission,
                                    "window": [fmt_hhmm(p.start), fmt_hhmm(p.end)],
                                    "mw": num(p.mw),
                                }
                            )
                impacts.append(
                    {
                        "test": test_id,
                        "corrections": corrections,
                        "certificate": cert.certificate,
                        "state": state,
                        "scope": {
                            "assembly": cert.assembly,
                            "start": fmt_hhmm(cert.start),
                            "end": fmt_hhmm(cert.end),
                        },
                        "new_holds_blocked": state == "under_review",
                        "accepted_volume_affected": affected,
                    }
                )
        return impacts
