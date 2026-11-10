"""Rendering for AgentPass ARI-100 reports.

Two outputs, both derived from the same report object the MCP server returns:

  * :func:`render_text` — a compact, terminal-and-LLM readable summary
  * :func:`prompt_pack` — an ordered remediation prompt, ready to paste into a
    coding agent

Everything here is defensive: a report missing a field renders a placeholder
instead of raising, because a partial report is still worth showing.
"""

from __future__ import annotations

from typing import Any


def _num(value: Any, default: Any = None) -> Any:
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) else default


def _fmt(value: Any, suffix: str = "") -> str:
    number = _num(value)
    if number is None:
        return "—"
    text = f"{number:.1f}".rstrip("0").rstrip(".") if isinstance(number, float) else str(number)
    return f"{text}{suffix}"


def _clean(text: Any) -> str:
    """Collapse whitespace in server-supplied prose so tables stay aligned."""
    return " ".join(str(text or "").split())


def score_line(report: dict) -> str:
    score = _fmt(report.get("score"))
    grade = report.get("grade") or "?"
    label = report.get("gradeLabel") or ""
    level = _fmt(report.get("level"))
    tail = f" — {label}" if label else ""
    return f"ARI-100 {score}/100 · grade {grade}{tail} · level L{level}"


def render_text(report: dict, *, max_fixes: int = 8) -> str:
    """Compact plain-text report: score, domains, crawler access, top fixes."""
    if not isinstance(report, dict):
        return "No report data returned."

    url = report.get("finalUrl") or report.get("url") or "(unknown url)"
    lines: list[str] = [f"Agent readiness — {url}", "  " + score_line(report)]

    counts = report.get("counts") or {}
    bits: list[str] = []
    if _num(counts.get("applicable")) is not None:
        bits.append(f"{_fmt(counts.get('applicable'))} of {_fmt(counts.get('total'))} checks scored")
    for key in ("pass", "partial", "fail"):
        if _num(counts.get(key)) is not None:
            bits.append(f"{_fmt(counts.get(key))} {key}")
    if bits:
        lines.append("  " + " · ".join(bits))

    signals: list[str] = []
    if _num(report.get("accessScore")) is not None:
        signals.append(f"crawler access {_fmt(report.get('accessScore'), '/100')}")
    if _num(report.get("a11yScore")) is not None:
        signals.append(f"accessibility {_fmt(report.get('a11yScore'), '/100')}")
    if _num(report.get("core")) is not None and _num(report.get("bonus")) is not None:
        signals.append(f"core {_fmt(report.get('core'))} + bonus {_fmt(report.get('bonus'))}")
    if signals:
        lines.append("  " + " · ".join(signals))
    if report.get("scannedAt"):
        lines.append(f"  scanned {report['scannedAt']}")

    domains = [d for d in (report.get("domains") or []) if isinstance(d, dict)]
    if domains:
        lines += ["", "Domains"]
        width = max((len(_clean(d.get("name"))) for d in domains), default=0)
        for domain in domains:
            name = _clean(domain.get("name")).ljust(width)
            passed = _fmt(domain.get("passed"))
            checks = _fmt(domain.get("checks"))
            weight = _num(domain.get("weight"))
            weight_txt = f"  w{_fmt(weight)}" if weight is not None else ""
            lines.append(f"  {_fmt(domain.get('score'), '%').rjust(6)}  {name}  {passed}/{checks}{weight_txt}")

    fixes = [f for f in (report.get("fixes") or []) if isinstance(f, dict)]
    recoverable = sum(_num(f.get("recoverable"), 0) or 0 for f in fixes)
    if fixes:
        head = f"Recoverable fixes — {len(fixes)} open, up to +{_fmt(round(recoverable, 1))} points"
        if report.get("grade") and fixes and recoverable:
            head += f" (current grade {report.get('grade')})"
        lines += ["", head]
        for index, fix in enumerate(fixes[:max_fixes], start=1):
            gain = _fmt(fix.get("recoverable"), " pts")
            ident = f"{_clean(fix.get('domain'))}/{_clean(fix.get('id'))}"
            lines.append(f"  {index:>2}. +{gain:<8} {ident} — {_clean(fix.get('title'))}")
            if fix.get("evidence"):
                lines.append(f"      {_clean(fix.get('evidence'))[:160]}")
        if len(fixes) > max_fixes:
            lines.append(f"  … {len(fixes) - max_fixes} more (call agent_readiness_fix_prompt for the ordered pack)")

    blocked = [
        c
        for c in (report.get("crawlers") or [])
        if isinstance(c, dict) and (c.get("blocked") or c.get("challenged") or str(c.get("verdict", "")).lower() not in ("", "allowed"))
    ]
    if blocked:
        lines += ["", "Crawlers not getting a clean 200"]
        for crawler in blocked[:6]:
            lines.append(
                f"  {_clean(crawler.get('ua')) or '?'} ({_clean(crawler.get('platform')) or '?'}): "
                f"{_clean(crawler.get('verdict')) or 'blocked'}"
            )

    return "\n".join(lines)


def prompt_pack(report: dict, *, max_items: int = 12) -> str:
    """Ordered remediation prompt for a coding agent.

    Ordering is by recoverable points (descending), then weight, then the
    server's own order — so the first item is always the cheapest real win.
    """
    if not isinstance(report, dict):
        return "No report data returned; re-run the scan."

    url = report.get("finalUrl") or report.get("url") or "(unknown url)"
    fixes = [f for f in (report.get("fixes") or []) if isinstance(f, dict)]
    ordered = sorted(
        enumerate(fixes),
        key=lambda pair: (-(_num(pair[1].get("recoverable"), 0) or 0), -(_num(pair[1].get("weight"), 0) or 0), pair[0]),
    )
    ordered = [f for _, f in ordered][:max_items]
    total = round(sum(_num(f.get("recoverable"), 0) or 0 for f in fixes), 1)

    lines = [
        f"You are fixing the agent readability of {url}.",
        f"Agent Readiness Index: {_fmt(report.get('score'))}/100, grade {report.get('grade') or '?'}"
        f" ({report.get('gradeLabel') or 'unrated'}), level L{_fmt(report.get('level'))}.",
        f"There are {len(fixes)} open findings worth up to +{_fmt(total)} points. Work top to bottom.",
        "",
        "Rules:",
        "- Change only what each item names. Do not restyle the site.",
        "- Do not invent facts: prices, dates, names and claims must already exist in the content.",
        "- After each item, re-run the named check id to confirm it moved from fail to pass.",
        "- Keep changes in one reviewable commit per domain where practical.",
        "",
        "Items:",
    ]

    if not ordered:
        lines.append("(no open findings — the last scan found nothing to fix)")

    for index, fix in enumerate(ordered, start=1):
        ident = f"{_clean(fix.get('domain'))}/{_clean(fix.get('id'))}"
        lines.append("")
        lines.append(
            f"{index}. [{ident}] +{_fmt(fix.get('recoverable'), ' pts')} (weight {_fmt(fix.get('weight'))}) — "
            f"{_clean(fix.get('title'))}"
        )
        if fix.get("evidence"):
            lines.append(f"   Observed: {_clean(fix.get('evidence'))}")
        body = _clean(fix.get("prompt"))
        if body:
            lines.append(f"   Fix: {body}")
        lines.append(f"   Verify: re-run check `{_clean(fix.get('id'))}` on {url}.")

    if len(fixes) > len(ordered):
        lines.append("")
        lines.append(f"({len(fixes) - len(ordered)} lower-value findings omitted.)")

    return "\n".join(lines)


def one_liner(report: dict) -> str:
    return f"{report.get('finalUrl') or report.get('url') or '?'} — {score_line(report)}"
