"""逐条业务规则的台账测试。运行：python3 -m unittest -v test_ledger"""
from __future__ import annotations

import json
import unittest
from pathlib import Path

from firmcap import build_report, load_pack
from firmcap.ledger import Ledger
from firmcap.model import (
    Assembly,
    Certification,
    Receipt,
    Segment,
    Submission,
    parse_hhmm,
)

DATA = Path(__file__).resolve().parent / "data"


def t(text: str) -> int:
    return parse_hhmm(text)


def build() -> Ledger:
    return load_pack(DATA)[0]


class EvidenceAndCertificationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ledger = build()

    def test_missing_grid_is_incomplete_never_pass_nor_zero(self):
        # 缺任何必需格都不能当成达标或零出力
        state = self.ledger.evidence_state("T-C", self.ledger.now)
        self.assertEqual(state["result"], "incomplete")
        g1710, g1720 = state["grids"]
        self.assertFalse(g1710["established"])
        self.assertIsNone(g1710["effective"])  # 缺证，不是 0 MW
        self.assertTrue(g1720["established"])
        self.assertEqual(g1720["effective"].mw, 30)
        # 引用证据不完整试验的认证不可用
        cert = Certification("C-X", "T-C", "upriver", t("18:00"), t("20:00"), 22.0,
                             t("17:40"), False)
        self.assertEqual(self.ledger.certification_state(cert, self.ledger.now),
                         "unsupported")

    def test_correction_flips_evidence_after_deadline(self):
        self.assertEqual(self.ledger.evidence_state("T-A", t("17:40"))["result"], "pass")
        self.assertEqual(self.ledger.evidence_state("T-A", self.ledger.now)["result"],
                         "fail")
        self.assertEqual(self.ledger.evidence_state("T-B", self.ledger.now)["result"],
                         "pass")

    def test_review_scoped_to_certifications_referencing_the_test(self):
        # 试验失败（更正）只能影响该试验引用的资格
        certs = self.ledger.certifications
        self.assertEqual(self.ledger.certification_state(certs["C-A"], self.ledger.now),
                         "under_review")
        self.assertEqual(self.ledger.certification_state(certs["C-B"], self.ledger.now),
                         "usable")
        self.assertEqual(self.ledger.certification_state(certs["C-0"], self.ledger.now),
                         "usable")
        # 挂复核在事件流中有据可查，且由计量更正触发
        changes = [e for e in self.ledger.events
                   if e["kind"] == "certification_state_changed"]
        self.assertEqual(len(changes), 1)
        self.assertEqual(changes[0]["certificate"], "C-A")
        self.assertEqual(changes[0]["cause"], {"measurement_grid": "17:20", "rev": 2})

    def test_dc_expansion_cannot_reuse_old_composition_certification(self):
        # 直流扩容投产是新组成：west-expanded 只能用 C-A，不沿用 west-old 的 C-0
        cap, ids, _ = self.ledger.qualification("west-expanded", t("18:30"), t("17:41"))
        self.assertEqual(cap, 32)
        self.assertEqual(ids, ["C-A"])
        cap_old, ids_old, _ = self.ledger.qualification("west-old", t("12:00"),
                                                        self.ledger.now)
        self.assertEqual(cap_old, 30)
        self.assertEqual(ids_old, ["C-0"])

    def test_previous_eligible_allows_carryover_when_granted(self):
        assemblies = {
            "a1": Assembly("S", "a1", "X", t("00:00"), t("10:00")),
            "a2": Assembly("S", "a2", "X", t("10:00"), t("20:00")),
        }
        ledger = Ledger(assemblies, {})
        ledger.apply_certification(
            Certification("C1", None, "a1", t("00:00"), t("20:00"), 10.0, t("00:00"), False))
        ledger.apply_certification(
            Certification("C2", None, "a2", t("12:00"), t("14:00"), 5.0, t("00:00"), True))
        cap, ids, _ = ledger.qualification("a2", t("13:00"), t("15:00"))
        self.assertEqual(cap, 10)
        self.assertEqual(set(ids), {"C1", "C2"})
        ledger2 = Ledger(assemblies, {})
        ledger2.apply_certification(
            Certification("C1", None, "a1", t("00:00"), t("20:00"), 10.0, t("00:00"), False))
        ledger2.apply_certification(
            Certification("C2", None, "a2", t("12:00"), t("14:00"), 5.0, t("00:00"), False))
        cap2, ids2, _ = ledger2.qualification("a2", t("13:00"), t("15:00"))
        self.assertEqual(cap2, 5)
        self.assertEqual(ids2, ["C2"])


class RequestAndHoldTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ledger = build()

    def test_all_or_nothing_rejection_lists_conflicting_windows_and_sources(self):
        st = self.ledger.submissions["R4-a"]
        self.assertEqual(st.status, "rejected")
        conflicts = st.rejection["conflicts"]
        self.assertEqual(len(conflicts), 3)
        by = {(c["window"]["start"], c["window"]["end"], c["constraint"]): c
              for c in conflicts}
        c1 = by[("18:20", "18:30", "connection_limit")]
        self.assertEqual((c1["capacity_mw"], c1["deficit_mw"], c1["limit_rev"]), (42, 2, 2))
        self.assertEqual([s["submission"] for s in c1["sources"]], ["R1-a"])
        c2 = by[("18:30", "18:40", "qualification")]
        self.assertEqual((c2["capacity_mw"], c2["deficit_mw"]), (24, 6))
        self.assertEqual([s["submission"] for s in c2["sources"]], ["R3-a"])
        c3 = by[("18:30", "18:40", "connection_limit")]
        self.assertEqual(c3["deficit_mw"], 18)
        self.assertEqual({s["submission"] for s in c3["sources"]}, {"R1-a", "R3-a"})
        # 整单拒绝：不留任何暂占
        self.assertFalse(any(o.submission == "R4-a"
                             for o in self.ledger.occupations(self.ledger.now)))

    def test_hold_expires_and_releases_capacity(self):
        expired = [e for e in self.ledger.events if e["kind"] == "hold_expired"]
        self.assertEqual([(e["submission"], e["hold_until"]) for e in expired],
                         [("R2-a", "17:50")])
        self.assertFalse(any(o.submission == "R2-a"
                             for o in self.ledger.occupations(t("17:51"))))

    def test_duplicate_acceptance_does_not_occupy_twice(self):
        st = self.ledger.submissions["R1-a"]
        self.assertEqual(st.accepted_receipt, "M18")
        self.assertEqual(st.duplicate_receipts, ["M18"])
        used = sum(o.mw for o in self.ledger.occupations(t("18:00"))
                   if o.connection == "G9" and o.covers(t("18:35")))
        self.assertEqual(used, 46)  # R1 30 + R3 16，重复受理没有再占一次

    def test_expired_hold_acceptance_is_pending_with_risks(self):
        st = self.ledger.submissions["R2-a"]
        self.assertIsNone(st.accepted_at)
        self.assertEqual(len(st.claims), 1)
        claim = st.claims[0]
        self.assertEqual(claim["receipt"], "M22")
        self.assertEqual([r["kind"] for r in claim["risks"]], ["hold_expired"])
        self.assertEqual(claim["risks"][0]["late_minutes"], 2)
        # 待核 claim 不占容量
        self.assertFalse(any(o.submission == "R2-a"
                             for o in self.ledger.occupations(self.ledger.now)))

    def test_accepted_shares_not_silently_released_by_review_or_limit_cut(self):
        # C-A 已挂复核、G9 限额已缩减，但 R1 已受理份额仍在占用
        occs = self.ledger.occupations(self.ledger.now)
        r1 = [o for o in occs if o.submission == "R1-a"]
        self.assertEqual(sorted((o.start, o.end, o.mw) for o in r1),
                         [(t("18:20"), t("18:30"), 30),
                          (t("18:30"), t("18:40"), 20),
                          (t("18:40"), t("18:50"), 25)])

    def test_reduction_adjusts_obligation_and_frees_explicitly(self):
        st = self.ledger.submissions["R1-a"]
        self.assertEqual(len(st.reductions), 1)
        red = st.reductions[0]
        self.assertEqual(red["receipt"], "M27")
        self.assertEqual([(f.start, f.end, f.mw) for f in red["freed"]],
                         [(t("18:30"), t("18:40"), 10)])
        pieces = self.ledger.pieces_as_of(st, self.ledger.now)
        self.assertEqual([(p.start, p.end, p.mw) for p in pieces],
                         [(t("18:20"), t("18:30"), 30),
                          (t("18:30"), t("18:40"), 20),
                          (t("18:40"), t("18:50"), 25)])

    def test_unknown_submission_receipt_is_unrecognized(self):
        ledger = build()
        ledger.apply_receipt(Receipt("X9", "NOPE", t("19:00"), "accepted", None))
        self.assertEqual(len(ledger.unrecognized_receipts), 1)
        self.assertIn("receipt_unrecognized",
                      [e["kind"] for e in ledger.events])

    def test_duplicate_submission_delivery_does_not_double_hold(self):
        ledger = build()
        before = len(ledger.occupations(t("17:45")))
        sub = ledger.submissions["R2-a"].submission
        ledger.apply_submission(sub)  # 内部重试 / 重复投递
        self.assertEqual(len(ledger.occupations(t("17:45"))), before)
        self.assertIn("submission_duplicate", [e["kind"] for e in ledger.events])

    def test_review_scope_blocks_new_holds(self):
        ledger = build()
        sub = Submission("R9", "R9-a", "west-expanded", t("17:57"), t("18:05"),
                         (Segment(t("19:00"), t("19:10"), 5.0),))
        ledger.apply_submission(sub)
        st = ledger.submissions["R9-a"]
        self.assertEqual(st.status, "rejected")
        kinds = {
            (c["constraint"], tuple(c["detail"].get("certificates", ())))
            for c in st.rejection["conflicts"]
        }
        self.assertIn(("qualification_under_review", ("C-A",)), kinds)


class ViewTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ledger = build()
        cls.now = cls.ledger.now

    def test_availability_now(self):
        rows = {(r["assembly"], tuple(r["window"])): r
                for r in self.ledger.availability(self.now)}
        self.assertEqual(rows[("east", ("18:00", "18:20"))]["available_mw"], 24)
        self.assertEqual(rows[("east", ("18:20", "18:30"))]["available_mw"], 12)
        self.assertEqual(rows[("east", ("18:30", "18:40"))]["available_mw"], 6)
        self.assertEqual(rows[("east", ("18:40", "18:50"))]["available_mw"], 1)
        tail = rows[("east", ("18:50", "20:00"))]
        self.assertEqual((tail["available_mw"], tail["blocked"]), (0, "no_published_limit"))
        west = rows[("west-expanded", ("18:30", "18:40"))]
        self.assertEqual(west["blocked"], "qualification_under_review")
        self.assertEqual(west["available_mw"], 0)
        up = rows[("upriver", ("18:00", "20:00"))]
        self.assertEqual(up["blocked"], "no_qualification")
        self.assertEqual(up["tests"], {"T-C": "incomplete"})
        # 待核 claim 在余量视图中可见但不占容量
        east_mid = rows[("east", ("18:30", "18:40"))]
        self.assertEqual(east_mid["pending_claims"],
                         [{"submission": "R2-a", "receipt": "M22", "claimed_mw": 18}])

    def test_stock_risks(self):
        risks = self.ledger.stock_risks(self.now)
        kinds = [r["kind"] for r in risks]
        self.assertEqual(kinds.count("pending_claim_misfit"), 2)
        self.assertEqual(kinds.count("accepted_backed_by_review"), 3)
        self.assertNotIn("connection_overcommitted", kinds)
        self.assertNotIn("qualification_overcommitted", kinds)
        misfit = [r for r in risks if r["kind"] == "pending_claim_misfit"][0]
        self.assertEqual(misfit["submission"], "R2-a")
        self.assertEqual(set(misfit["problems"]),
                         {"qualification_exceeded", "connection_limit_exceeded"})
        backed = [r for r in risks if r["kind"] == "accepted_backed_by_review"]
        self.assertEqual({r["submission"] for r in backed}, {"R1-a"})
        self.assertEqual(sum(r["mw"] for r in backed), 75)

    def test_delivery_watchlist(self):
        watch = {w["submission"]: w for w in self.ledger.delivery_watchlist(self.now)}
        self.assertEqual(set(watch), {"R1-a", "R2-a", "R3-a"})
        r1k = {r["kind"] for r in watch["R1-a"]["reasons"]}
        self.assertEqual(r1k, {"qualification_under_review", "obligation_reduced",
                               "missing_delivery_evidence"})
        r2k = {r["kind"] for r in watch["R2-a"]["reasons"]}
        self.assertEqual(r2k, {"acceptance_pending_review", "shared_delivery_actuals"})
        r3k = {r["kind"] for r in watch["R3-a"]["reasons"]}
        self.assertEqual(r3k, {"shared_delivery_actuals"})

    def test_reconciliation_distinguishes_missing_deviation_reduced(self):
        rows = {(r["submission"], r["grid"]): r
                for r in self.ledger.reconciliation(self.now)}
        r1a = rows[("R1-a", "18:20")]
        self.assertEqual(r1a["result"], "missing_evidence")
        r1b = rows[("R1-a", "18:30")]
        self.assertEqual(r1b["result"], "shortfall")
        self.assertEqual(r1b["deviation_mw"], 2)          # 对已调减的 20 而非原 30
        self.assertEqual(r1b["obligation_original_mw"], 30)
        self.assertEqual(r1b["obligation_effective_mw"], 20)
        self.assertEqual(r1b["reductions_applied"], ["M27"])
        self.assertEqual(r1b["actual_mw"], 18)
        self.assertEqual(rows[("R1-a", "18:40")]["result"], "missing_evidence")
        r3a = rows[("R3-a", "18:30")]
        self.assertEqual((r3a["result"], r3a["surplus_mw"]), ("fulfilled", 3))
        r2a = rows[("R2-a", "18:30")]
        self.assertEqual(r2a["basis"], "pending_review")
        self.assertEqual(r2a["shared_with"], ["R3-a"])

    def test_correction_impact_chain(self):
        impacts = self.ledger.correction_impacts(self.now)
        self.assertEqual(len(impacts), 1)
        imp = impacts[0]
        self.assertEqual(imp["test"], "T-A")
        self.assertEqual(imp["corrections"],
                         [{"grid": "17:20", "from": {"mw": 33, "rev": 1},
                           "to": {"mw": 27, "rev": 2}, "received_at": "17:56",
                           "evidence_deadline": "17:35"}])
        self.assertEqual(imp["certificate"], "C-A")
        self.assertEqual(imp["state"], "under_review")
        self.assertTrue(imp["new_holds_blocked"])
        self.assertEqual(imp["scope"],
                         {"assembly": "west-expanded", "start": "18:00", "end": "20:00"})
        affected = {(a["submission"], tuple(a["window"])): a["mw"]
                    for a in imp["accepted_volume_affected"]}
        self.assertEqual(affected, {("R1-a", ("18:20", "18:30")): 30,
                                    ("R1-a", ("18:30", "18:40")): 20,
                                    ("R1-a", ("18:40", "18:50")): 25})

    def test_why_snapshot_explains_past_declarability(self):
        why = self.ledger.why("R1-a")
        self.assertEqual(why["status"], "accepted")
        first = why["evaluation"][0]
        self.assertEqual(first["qualification"]["certificates"], ["C-A"])
        self.assertEqual(first["qualification"]["capacity_mw"], 32)
        self.assertEqual(first["connection"]["limit_mw"], 48)
        self.assertEqual(first["connection"]["limit_rev"], 1)
        self.assertEqual(why["evidence"]["T-A"]["result"], "pass")
        self.assertEqual(why["acceptance"]["receipt"], "M18")
        self.assertEqual(why["acceptance"]["duplicate_receipts"], ["M18"])

    def test_report_is_json_serializable(self):
        report = build_report(self.ledger, {"input_pack": "firm-capacity", "tables": {}})
        json.dumps(report, ensure_ascii=False)
        self.assertEqual(report["generated_at"], "18:53")


if __name__ == "__main__":
    unittest.main()
