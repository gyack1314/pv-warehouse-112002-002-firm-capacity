"""命令行入口：装载资料包，回放事件，输出申报数据与履约依据。

用法：
    python3 run_pipeline.py [--data data] [--out report.json]
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from firmcap import build_report, load_pack


def _summarize(report: dict) -> str:
    lines = []
    lines.append(f"资料包 {report['input_pack'].get('input_pack')} 校验通过，"
                 f"事件 {len(report['event_log'])} 条，截至 {report['generated_at']}")
    lines.append("== 试验证据 ==")
    for t in report["tests"]:
        lines.append(f"  {t['test']} ({t['assembly']}): {t['result']}"
                     f"  阈值 {t['firm_threshold_mw']} MW")
    lines.append("== 认证 ==")
    for c in report["certifications"]:
        lines.append(f"  {c['certificate']} ({c['assembly']} {c['window'][0]}-{c['window'][1]}"
                     f" {c['max_mw']} MW): {c['state']}")
    lines.append("== 申报 ==")
    for s in report["submissions"]:
        extra = ""
        if s["status"] == "rejected":
            extra = f"，冲突 {len(s['rejection']['conflicts'])} 处"
        elif "pending_claims" in s:
            extra = "，有待核受理"
        lines.append(f"  {s['submission']} ({s['assembly']}): {s['status']}{extra}")
    lines.append("== 存量风险 ==")
    for r in report["stock_risks"]:
        lines.append(f"  [{r['kind']}] {json.dumps(r, ensure_ascii=False, sort_keys=True)}")
    lines.append("== 交割需单独对照 ==")
    for w in report["delivery_watchlist"]:
        kinds = [r["kind"] for r in w["reasons"]]
        lines.append(f"  {w['submission']}: {', '.join(kinds)}")
    lines.append("== 交割核对 ==")
    for r in report["reconciliation"]:
        lines.append(
            f"  {r['submission']} {r['grid']}: 义务 {r['obligation_effective_mw']} MW"
            f"（原 {r['obligation_original_mw']}），实测 {r['actual_mw']} -> {r['result']}"
        )
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data", type=Path, default=Path(__file__).resolve().parent / "data")
    parser.add_argument("--out", type=Path, default=None, help="完整 JSON 报告输出路径")
    args = parser.parse_args()

    ledger, pack_info = load_pack(args.data)
    report = build_report(ledger, pack_info)
    print(_summarize(report))
    if args.out is not None:
        args.out.write_text(
            json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        print(f"\n完整报告已写入 {args.out}")


if __name__ == "__main__":
    main()
