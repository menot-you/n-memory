"""Report generator for comparative memory benchmark results."""

from __future__ import annotations

from nmemory_evals.comparison.runner import ComparisonReport


def render_markdown_matrix(report: ComparisonReport) -> str:
    """Renders formatted GitHub-flavored markdown comparison table."""
    lines = [
        f"# Comparative Benchmark of Agent Memory Systems (Gold Set {report.total_items} items)",
        "",
        "| System | Recall@1 | Recall@5 | Grounded | Ingest (ms/item) | Query (ms) | Provenance | Cryptographic Audit | Abstain Outcome |",
        "|---|:---:|:---:|:---:|:---:|:---:|:---:|:---:|:---:|",
    ]

    for s in report.systems:
        prov = "✓ Yes (`path:line`)" if s.provenance_preserved else "✗ No (summary)"
        audit = "✓ Yes (Hash-Chain)" if s.tamper_evident_audit else "✗ No"
        abstain = (
            "✓ Yes (3 outcomes)" if s.explicit_abstain_support else "✗ No (empty only)"
        )
        name_display = (
            f"**{s.system.value.upper()}**"
            if s.system.value == "nmemory"
            else s.system.value.capitalize()
        )

        lines.append(
            f"| {name_display} | {s.recall_at_1 * 100:.1f}% | {s.recall_at_5 * 100:.1f}% | {s.grounded_rate * 100:.1f}% | "
            f"{s.avg_ingest_latency_ms:.1f} | {s.avg_query_latency_ms:.1f} | {prov} | {audit} | {abstain} |"
        )

    lines.extend(
        [
            "",
            "### Breakdown by Category",
            "",
        ]
    )

    for s in report.systems:
        lines.append(f"#### {s.system.value.capitalize()}")
        for cat, stats in s.category_scores.items():
            lines.append(
                f"- **{cat}** (N={int(stats['total'])}): "
                f"Recall@1 = {stats['recall_at_1'] * 100:.1f}%, "
                f"Recall@3 = {stats['recall_at_3'] * 100:.1f}%, "
                f"Recall@5 = {stats['recall_at_5'] * 100:.1f}%"
            )
        lines.append("")

    return "\n".join(lines)
