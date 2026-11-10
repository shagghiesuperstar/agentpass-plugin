"""Model-facing tool schemas.

The `description` is what the model reads to decide when to call. It names the
user phrasings that should trigger the tool, what the tool returns, and what it
does *not* do — the three things that most often cause a mis-call.
"""

SCAN = {
    "name": "agent_readiness_scan",
    "description": (
        "Scan a public website and score how readable it is to AI agents and answer engines "
        "(Agent Readiness Index, ARI-100). Returns the score, grade, per-domain breakdown, "
        "crawler access verdicts (which AI crawlers get a clean 200, which are blocked or "
        "challenged), accessibility, and the recoverable-point fixes. "
        "Use when the user asks whether a site is ready for AI agents / LLMs / ChatGPT / "
        "Perplexity, how it scores on AEO, GEO, AI SEO or agent readiness, whether AI crawlers "
        "can reach it, or for an audit of llms.txt, robots.txt, structured data, and agent "
        "interfaces. `format='text'` (default) returns a readable report; `format='json'` "
        "returns the full machine-readable report object. This tool only reads the site; it "
        "changes nothing."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "url": {
                "type": "string",
                "description": (
                    "Site to scan. A full URL (https://example.com) or a bare host "
                    "(example.com); https is assumed when the scheme is omitted."
                ),
            },
            "format": {
                "type": "string",
                "enum": ["text", "json"],
                "default": "text",
                "description": (
                    "text: compact human-readable report (score, domains, top fixes). "
                    "json: the full ARI-100 report object, for filtering or charting."
                ),
            },
        },
        "required": ["url"],
        "additionalProperties": False,
    },
}

FIX_PROMPT = {
    "name": "agent_readiness_fix_prompt",
    "description": (
        "Produce an ordered, paste-ready remediation prompt that fixes a site's AI-agent "
        "readability, based on a live Agent Readiness Index scan. Each numbered item carries "
        "the observed evidence, the exact fix, and the check id to re-run for verification, "
        "sorted so the cheapest real points come first. "
        "Use when the user asks how to fix / improve agent readiness, wants a prompt for a "
        "coding agent (Claude Code, Codex, Cursor) to make a site agent-readable, or asks "
        "'what should I change first'. Pass the same `url` you would give "
        "agent_readiness_scan. The scan is reused from cache when it is younger than the "
        "cache TTL, so calling scan then fix_prompt costs one network round trip."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "url": {
                "type": "string",
                "description": "Site to remediate. Full URL or bare host; https is assumed.",
            },
            "max_items": {
                "type": "integer",
                "minimum": 1,
                "maximum": 40,
                "default": 12,
                "description": "Cap on numbered items in the prompt (default 12, highest-value first).",
            },
        },
        "required": ["url"],
        "additionalProperties": False,
    },
}
