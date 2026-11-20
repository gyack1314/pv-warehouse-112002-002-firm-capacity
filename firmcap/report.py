"""把台账视图汇总为可核验的申报数据与履约依据（JSON 友好）。"""
from __future__ import annotations

from .ledger import Ledger
from .model import fmt_hhmm, num


def _evidence_json(ledger: Ledger, test_id: str, tau: int) -> dict:
    window = ledger.tests[test_id]
    state = ledger.evidence_state(test_id, tau)
    grids = []
    for g in state["grids"]:
        grids.append(
            {
                "at": fmt_hhmm(g["at"]),
                "established": g["established"],
                "effective_mw": num(g["effective"].mw) if g["effective"] else None,
                "revisions": [
                    {
                        "mw": num(m.mw),
                        "rev": m.rev,
                        "received_at": fmt_hhmm(m.received_at),
                        "timely": m.received_at <= window.evidence_deadline,
                    }
                    for m in g["revisions"]
                ],
            }
        )
    return {
        "test": test_id,
        "assembly": window.assembly,
        "window": [fmt_hhmm(window.start), fmt_hhmm(window.end)],
        "grid_minutes": window.grid_minutes,
        "evidence_deadline": fmt_hhmm(window.evidence_deadline),
        "firm_threshold_mw": num(window.firm_threshold),
        "grids": grids,
        "result": state["result"],
    }


def build_report(ledger: Ledger, pack_info: dict) -> dict:
    now = ledger.now

    tests = []
    for test_id in sorted(ledger.tests):
        entry = _evidence_json(ledger, test_id, now)
        entry["result_at_certification"] = {
            c.certificate: ledger.evidence_state(test_id, c.decided_at)["result"]
            for c in ledger.certifications.values()
            if c.test == test_id
        }
        tests.append(entry)

    certifications = []
    for c in sorted(ledger.certifications.values(), key=lambda c: c.certificate):
        state = ledger.certification_state(c, now)
        certifications.append(
            {
                "certificate": c.certificate,
                "test": c.test,
                "assembly": c.assembly,
                "window": [fmt_hhmm(c.start), fmt_hhmm(c.end)],
                "max_mw": num(c.max_mw),
                "decided_at": fmt_hhmm(c.decided_at),
                "previous_eligible": c.previous_eligible,
                "state": state,
            }
        )

    connections: dict[str, list] = {}
    for l in ledger.limits:
        connections.setdefault(l.connection, []).append(
            {
                "window": [fmt_hhmm(l.start), fmt_hhmm(l.end)],
                "mw": num(l.mw),
                "published_at": fmt_hhmm(l.published_at),
                "rev": l.rev,
            }
        )

    submissions = []
    for key in sorted(ledger.submissions):
        entry = ledger.why(key)
        st = ledger.submissions[key]
        if st.accepted_at is not None:
            entry["obligations"] = [
                ledger._seg_json(p) for p in ledger.pieces_as_of(st, now)
            ]
        submissions.append(entry)

    return {
        "generated_at": fmt_hhmm(now),
        "input_pack": pack_info,
        "tests": tests,
        "certifications": certifications,
        "connection_limits": connections,
        "submissions": submissions,
        "availability_now": ledger.availability(now),
        "stock_risks": ledger.stock_risks(now),
        "delivery_watchlist": ledger.delivery_watchlist(now),
        "reconciliation": ledger.reconciliation(now),
        "correction_impacts": ledger.correction_impacts(now),
        "unrecognized_receipts": [
            {"receipt": r.receipt, "submission": r.submission, "at": fmt_hhmm(r.at)}
            for r in ledger.unrecognized_receipts
        ],
        "event_log": ledger.events,
    }
