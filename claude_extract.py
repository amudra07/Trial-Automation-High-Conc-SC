"""
claude_extract.py

The Claude-powered search + extraction step. Calls the Claude API with
the web search tool once per category, and forces the response into
the structured schema db.py / app.py expect.

Fix for the "Could not resolve authentication method" error:
that error means the Anthropic client found no API key at all --
neither an environment variable nor Streamlit secrets. get_api_key()
below checks both places explicitly and raises a clear, specific
MissingAPIKeyError instead of letting the SDK's generic error surface,
so app.py can show one clean instructional message instead of 4 junk
"search error" candidate cards.
"""

import json
import os
import re

import anthropic

try:
    import streamlit as st
except ImportError:
    st = None

MODEL = "claude-sonnet-5"

CATEGORIES = [
    "Liquid + excipient",
    "Enzyme co-formulation",
    "Suspension / particle",
    "Crystalline",
]


class MissingAPIKeyError(Exception):
    pass


def get_api_key() -> str:
    """Checks Streamlit secrets first (how Streamlit Cloud injects them),
    then falls back to a plain environment variable (how you'd run this
    locally). Raises a clear error if neither is set, instead of letting
    the Anthropic SDK's generic auth error bubble up."""
    if st is not None:
        try:
            key = st.secrets.get("ANTHROPIC_API_KEY")
            if key:
                return key
        except Exception:
            pass  # no secrets.toml configured locally -- fine, fall through
    key = os.environ.get("ANTHROPIC_API_KEY")
    if key:
        return key
    raise MissingAPIKeyError(
        "No ANTHROPIC_API_KEY found. On Streamlit Cloud: app Settings -> "
        "Secrets -> add ANTHROPIC_API_KEY = \"your-key\". Locally: "
        "export ANTHROPIC_API_KEY=your-key before running streamlit."
    )


def get_client() -> anthropic.Anthropic:
    return anthropic.Anthropic(api_key=get_api_key())


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


def search_category(category: str, client: anthropic.Anthropic) -> list[dict]:
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
    """Raises MissingAPIKeyError immediately if the key isn't set --
    fails fast with one clear message instead of burning 4 category
    searches only to fail identically 4 times."""
    client = get_client()
    all_candidates = []
    for category in CATEGORIES:
        try:
            all_candidates.extend(search_category(category, client=client))
        except MissingAPIKeyError:
            raise
        except Exception as exc:
            all_candidates.append({
                "id": f"error-{category}".replace(" ", "-").lower(),
                "name": f"Search error in {category}",
                "developer": "N/A",
                "category": category,
                "phase": "Preclinical",
                "concentration_mgml": None,
                "concentration_type": None,
                "concentration_display": f"Search failed: {exc}",
                "needle_size_g": "Not disclosed",
                "mechanism_short": "",
                "mechanism_long": "This candidate represents a failed search, not a real technology.",
                "deals": [],
                "source_name": "N/A",
                "source_url": None,
                "confidence": 0.0,
            })
    return all_candidates
