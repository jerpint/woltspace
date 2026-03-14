"""
🐻 Bear — Safety & Validation

The bear guards the den. It reviews outputs, flags risks, and enforces boundaries
before anything goes out into the world.

Role:
  - Post-generation validation (check outputs before they're shown or committed)
  - Safety checks: PII detection, secret scanning, policy enforcement
  - Quality gates: "is this HTML valid?", "does this code do what it says?"
  - Invoked per-task, not continuous — bear reviews, then steps back

Design:
  - Callable as a step in any pipeline: output → bear.review() → pass/flag
  - Returns: {"ok": bool, "issues": [...], "severity": "low|medium|high"}
  - Severity "high" = block the output; "medium" = warn; "low" = log only
  - Uses an LLM (sonnet) for semantic checks; rule-based for obvious patterns
  - Does NOT modify outputs — only annotates them

Entry point:
  - Library: `from creatures.bear import review`
  - CLI: `python -m creatures.bear --input <file>` (for manual review)
  - Or a bot tool: `bear_review(content, context)` → issues list

TODO (implementation):
  - Define review schema (issue types, severity levels)
  - Implement rule-based checks (regex for secrets, PII patterns)
  - Add LLM review call (sonnet, low-temp, structured output)
  - Hook into bot tool registry as an optional post-processing step
  - Test: bear correctly flags hardcoded tokens, PII, clearly wrong code
"""

# Placeholder — not yet implemented


def review(content: str, context: str = "") -> dict:
    raise NotImplementedError("bear is not yet implemented")
