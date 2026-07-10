"""
claude_extract.py

The "heavy-duty tracking" step: calls the Claude API with the web search
tool to find new high-concentration SC delivery signals, and forces the
response into the structured schema our db.py / app.py expect.

Design notes:
  - concentration is captured with a `concentration_type` qualifier
    (achieved_clinical / achieved_preclinical / target / theoretical)
    because a bare mg/mL number is misleading without it -- this was
    flagged as the single most important field earlier.
  - one call per category rather than one giant prompt, since a single
    broad prompt tends to under-cover a niche field like this one.
  - confidence is self-reported by the model and should be treated as a
    coarse triage signal, not ground truth -- route anything below your
    threshold to human review rather than trusting it outright.
"""

import json
import os
import re

import anthropic

MODEL = "claude-sonnet-5"

CATEGORIES = [
    "Liquid + excipient",
    "Enzyme co-formulation",
    "Suspension / particle",
    "Crystalline",
]

SYSTEM_PROMPT = """You are a competitive-intelligence research assistant for a \
pharmaceutical R&D team tracking high-concentration subcutaneous (SC) delivery \
technology for monoclonal antibodies (mAbs) and antibody-drug conjugates (ADCs).

Use web search to find recent, real, verifiable developments in the given \
technology category. For each distinct competitor technology or program you find, \
extract a structured record.

Critical rules:
- concentration_mgml: the numeric mg/mL value if one is stated. Never invent a number.
- concentration_type: one of "achieved_clinical" (demonstrated in a clinical trial or \
approved product), "achieved_preclinical" (demonstrated in animal/bench studies), \
"target" (a stated goal not yet demonstrated), "theoretical" (modeled/claimed without data), \
or null if no number is available.
- phase: one of Proof-of-concept, Platform PoC, Preclinical, Phase 1, Phase 3, \
Commercial, CDMO service available.
- Only include developments with a real, findable source. Every record must include \
source_name and source_url from an actual search result -- never fabricate a citation.
- confidence: your own estimate (0.0-1.0) of how well-supported this record is, \
considering source quality and whether the number/stage is explicitly stated vs. inferred.
- Do not repeat well-established facts you already reported in a previous search unless \
something has changed -- focus on what's new or updated.

Respond with ONLY a JSON array (no markdown fences, no prose before or after). Each \
element must have exactly these keys: id (short slug), name, developer, category, phase, \
concentration_mgml (number or null), concentration_type (string or null), \
concentration_display (short human-readable string), needle_size_g (string, \
"Not disclosed" if unknown), mechanism_short, mechanism_long, deals (array of strings, \
can be empty), source_name, source_url, confidence.

If you find nothing new or verifiable, respond with an empty JSON array: []
"""


def _extract_json_array(text: str) -> list[dict]:
    text = text.strip()
    text = re.sub(r"^```(json)?|```$", "", text, flags=re.MULTILINE).strip()
    match = re.search(r"\[.*\]", text, flags=re.DOTALL)
    if not match:
        return []
    try:
        return json.loads(match.group(0))
    except json.JSONDecodeError:
        return []


def search_category(category: str, client: anthropic.Anthropic | None = None) -> list[dict]:
    """Runs one search+extract pass for a single category. Returns a
    list of candidate dicts ready for db.add_staging_entries()."""
    client = client or anthropic.Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY"))

    user_prompt = (
        f"Search for recent developments (last 6 months) in the '{category}' "
        f"category of high-concentration SC delivery technology for mAbs/ADCs. "
        f"Look for: new concentration figures, stage progression (e.g. entering "
        f"Phase 1), new licensing deals, and new entrants. Extract structured "
        f"records per the schema in your instructions."
    )

    response = client.messages.create(
        model=MODEL,
        max_tokens=4096,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": user_prompt}],
        tools=[{"type": "web_search_20250305", "name": "web_search"}],
    )

    final_text = "\n".join(
        block.text for block in response.content if getattr(block, "type", None) == "text"
    )
    candidates = _extract_json_array(final_text)

    for c in candidates:
        c.setdefault("category", category)
        c["confidence"] = float(c.get("confidence", 0.5))
    return candidates


def search_all_categories() -> list[dict]:
    """Runs a search pass across every category. This is the function
    the 'Refresh intelligence' button in app.py calls. Expect this to
    take on the order of tens of seconds to a couple of minutes -- run
    it with a spinner, don't expect it to feel instant."""
    client = anthropic.Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY"))
    all_candidates = []
    for category in CATEGORIES:
        try:
            all_candidates.extend(search_category(category, client=client))
        except Exception as exc:
            # Don't let one category's failure kill the whole run.
            all_candidates.append({
                "id": f"error-{category}",
                "name": f"[Search error in {category}]",
                "developer": "N/A",
                "category": category,
                "phase": "Preclinical",
                "concentration_mgml": None,
                "concentration_type": None,
                "concentration_display": f"Search failed: {exc}",
                "mechanism_short": "",
                "mechanism_long": "",
                "deals": [],
                "source_name": "",
                "source_url": None,
                "confidence": 0.0,
            })
    return all_candidates
