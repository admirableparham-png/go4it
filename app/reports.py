"""Phase 8 — internal admin reports (CSV + sandboxed PDF), private + audited.

A report bundles aggregate metrics (with their exact definitions), the commercial funnel, and a source-freshness
appendix. Currencies are kept SEPARATE (never summed). Reports are AGGREGATE — no buyer/seller PII. They are
stored privately, hashed, and generation/download are audited. No report is ever auto-emailed. PDFs render
through the hardened Phase-5/6 sandbox (production Chromium gate applies).
"""
import csv
import hashlib
import io
import json
import os
import re
from datetime import datetime
from pathlib import Path

from . import analytics as ANALYTICS
from . import data_sources as DATASRC
from . import metrics as M
from . import pdf_render as PDF
from .models import AnalyticsReport
from .opportunity_scoring import scoring_version
from .pipeline import audit

REPORT_TYPES = ("weekly_exec", "monthly_demand", "market", "category", "source_quality", "funnel",
                "operations", "team_performance")

# which metrics each report headlines (aggregate only — never a buyer identifier)
_REPORT_METRICS = {
    "weekly_exec": ["active_requests", "open_work_queue", "positive_replies", "quotes_accepted",
                    "active_deals", "settled_value", "operational_exceptions"],
    "monthly_demand": ["researched_prospects", "contactable_prospects", "human_replies", "positive_replies",
                       "buyer_requirements", "quotes_accepted"],
    "market": ["quotes_sent", "quotes_accepted", "deals_opened", "deals_delivered"],
    "category": ["quotes_sent", "quotes_accepted", "deals_opened"],
    "source_quality": ["researched_prospects", "contactable_prospects", "human_replies", "positive_replies"],
    "funnel": ["researched_prospects", "positive_replies", "quotes_accepted", "deals_opened", "deals_settled"],
    "operations": ["active_shipments", "operational_exceptions", "deals_delivered", "settled_value",
                   "payments_received"],
    "team_performance": ["quotes_accepted", "deals_delivered", "open_work_queue", "overdue_work_items"],
}


def _safe_name(name: str) -> str:
    base = os.path.basename(name or "report")
    return (re.sub(r"[^A-Za-z0-9._-]", "_", base).lstrip(".") or "report")[:120]


def build_data(session, report_type, *, since=None, until=None, filters=None, now=None):
    """Assemble the aggregate report payload + a metric-definition and source-freshness appendix."""
    now = now or datetime.utcnow()
    keys = _REPORT_METRICS.get(report_type, ["researched_prospects"])
    metric_rows = []
    for k in keys:
        val = M.compute(k, session, since=since, until=until)
        metric_rows.append({"key": k, "label": M.definition(k)["label"], "value": val,
                            "definition": M.definition(k)["definition"], "formula": M.definition(k)["formula"],
                            "excluded": M.definition(k)["excluded"], "per_currency": isinstance(val, dict)})
    fnl = ANALYTICS.funnel(session, since=since, until=until)
    sources = [{"label": s["label"], "freshness": s["freshness"], "last_success":
                s["last_success"].isoformat() if s["last_success"] else None} for s in DATASRC.source_health(session)]
    return {
        "report_type": report_type, "generated_at": now.isoformat(),
        "time_range": ANALYTICS._range_label(since, until), "filters": filters or {},
        "metric_version": M.METRIC_VERSION, "scoring_version": scoring_version(),
        "metrics": metric_rows, "funnel": fnl, "source_freshness": sources,
    }


def _to_csv(data) -> bytes:
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["section", "key", "value", "note"])
    w.writerow(["meta", "report_type", data["report_type"], ""])
    w.writerow(["meta", "time_range", data["time_range"], ""])
    w.writerow(["meta", "generated_at", data["generated_at"], ""])
    w.writerow(["meta", "metric_version", data["metric_version"], ""])
    w.writerow(["meta", "scoring_version", data["scoring_version"], ""])
    for m in data["metrics"]:
        if m["per_currency"]:
            for ccy, amt in (m["value"] or {}).items():   # currencies stay on their own rows — never summed
                w.writerow(["metric", f"{m['key']}[{ccy}]", amt, m["definition"]])
        else:
            w.writerow(["metric", m["key"], m["value"], m["definition"]])
    for st in data["funnel"]["stages"]:
        w.writerow(["funnel", st["key"], st["count"],
                    f"conversion={st['conversion']}" if st["conversion"] is not None else "n/a"])
    for s in data["source_freshness"]:
        w.writerow(["source", s["label"], s["freshness"], s["last_success"] or ""])
    return buf.getvalue().encode()


def _to_html(data) -> str:
    e = PDF.escape
    rows = ""
    for m in data["metrics"]:
        if m["per_currency"]:
            v = " · ".join(f"{e(c)} {e(a)}" for c, a in (m["value"] or {}).items()) or "—"
        else:
            v = e(m["value"])
        rows += f"<tr><td>{e(m['label'])}</td><td class='n'>{v}</td><td class='d'>{e(m['definition'])}</td></tr>"
    fnl = "".join(f"<tr><td>{e(s['label'])}</td><td class='n'>{s['count']}</td>"
                  f"<td class='n'>{e(s['conversion']) if s['conversion'] is not None else 'n/a'}</td></tr>"
                  for s in data["funnel"]["stages"])
    src = "".join(f"<tr><td>{e(s['label'])}</td><td>{e(s['freshness'])}</td></tr>"
                  for s in data["source_freshness"])
    return f"""<!doctype html><html><head><meta charset='utf-8'><style>
      body{{font-family:Arial,Helvetica,sans-serif;color:#111;margin:28px;font-size:12px}}
      h1{{font-size:19px;margin:0 0 2px}} .sub{{color:#666;margin-bottom:14px}}
      table{{border-collapse:collapse;width:100%;margin:10px 0}} th,td{{border:1px solid #ddd;padding:5px 7px;text-align:left}}
      th{{background:#f4f4f4}} td.n{{text-align:right;font-variant-numeric:tabular-nums}} td.d{{color:#666;font-size:11px}}
      .appendix{{color:#666;font-size:11px;margin-top:14px}}
    </style></head><body>
      <h1>Go4it — {e(data['report_type'].replace('_',' ').title())} report</h1>
      <div class='sub'>Range {e(data['time_range'])} · generated {e(data['generated_at'])} ·
        metric {e(data['metric_version'])} · scoring {e(data['scoring_version'])}</div>
      <table><thead><tr><th>Metric</th><th>Value</th><th>Definition</th></tr></thead><tbody>{rows}</tbody></table>
      <h3>Commercial funnel</h3>
      <table><thead><tr><th>Stage</th><th>Count</th><th>Conversion</th></tr></thead><tbody>{fnl}</tbody></table>
      <p class='sub'>{e(data['funnel']['basis'])}</p>
      <h3>Source freshness (appendix)</h3>
      <table><thead><tr><th>Source</th><th>Freshness</th></tr></thead><tbody>{src}</tbody></table>
      <p class='appendix'>Aggregate internal report — no buyer/seller identity. Currencies are never summed.
        Not legal or financial advice.</p>
    </body></html>"""


def _ref(row_id, now):
    return f"RPT-{now:%Y%m}-{row_id:04d}"


def generate(session, *, report_type, files_dir, since=None, until=None, filters=None, fmt="csv", actor=None,
             now=None):
    """Generate + store a report privately. Returns (report, error). CSV always works; PDF degrades to a failed
    row (+ a Work Queue task) if the sandbox/Chromium is unavailable — never crashes the request."""
    now = now or datetime.utcnow()
    if report_type not in REPORT_TYPES:
        return None, "unknown report type"
    data = build_data(session, report_type, since=since, until=until, filters=filters, now=now)
    rpt = AnalyticsReport(report_type=report_type, title=report_type.replace("_", " ").title(),
                          time_range=data["time_range"], filters=json.dumps(filters or {})[:2000],
                          params=json.dumps({"fmt": fmt})[:500], metric_version=data["metric_version"],
                          scoring_version=data["scoring_version"],
                          source_freshness=json.dumps(data["source_freshness"])[:4000],
                          generated_by=getattr(actor, "id", None), generated_at=now, status="generated")
    session.add(rpt)
    session.flush()
    rpt.reference = _ref(rpt.id, now)
    root = Path(files_dir)
    root.mkdir(parents=True, exist_ok=True)
    err = ""
    if fmt == "pdf":
        html = _to_html(data)
        fname = f"{rpt.id}_{_safe_name(report_type)}.pdf"
        path = (root / fname).resolve()
        if not str(path).startswith(str(root.resolve()) + os.sep):
            return None, "unsafe path"
        ok, perr = PDF.render_pdf(html, path)
        if not ok:
            rpt.status = "failed"
            session.add(rpt)
            _failed_task(session, rpt, perr)
            audit(session, actor, "analytics_report", rpt.id, "report_generation_failed", {"error": perr[:200]})
            return rpt, perr or "pdf render failed"
        rpt.content_type = "application/pdf"
        rpt.file_path = fname
        rpt.sha256 = PDF.sha256_file(path)
    else:
        blob = _to_csv(data)
        fname = f"{rpt.id}_{_safe_name(report_type)}.csv"
        path = (root / fname).resolve()
        if not str(path).startswith(str(root.resolve()) + os.sep):
            return None, "unsafe path"
        path.write_bytes(blob)
        rpt.content_type = "text/csv"
        rpt.file_path = fname
        rpt.sha256 = hashlib.sha256(blob).hexdigest()
    session.add(rpt)
    audit(session, actor, "analytics_report", rpt.id, "report_generated",
          {"type": report_type, "fmt": fmt, "sha256": rpt.sha256})
    return rpt, err


def _failed_task(session, rpt, perr):
    try:
        from . import work_queue as WQ
        WQ.create_work_item_safe(
            session, tenant_id=None, type="report_generation_failed", source="automatic",
            title=f"Report {rpt.reference} generation failed",
            description=(perr or "PDF sandbox unavailable")[:400],
            idempotency_key=f"report_generation_failed:rpt:{rpt.id}", condition_version="failed")
    except Exception:  # noqa: BLE001
        pass
