"""Optional analyst layer: Claude reads the numeric brief and writes a short interpretation.

The brief is computed by code (analyze.py). Claude only interprets it, and is told to use no
figures that are not already in the brief. Runs only when ANTHROPIC_API_KEY is set.
"""
import os

SYSTEM = (
    "You are the analyst layer of a trend-monitoring pipeline. You receive a data brief that code "
    "computed from Google Trends, Wikipedia, YouTube and Reddit numbers. Write a short 'Analyst read' "
    "of at most 220 words in plain prose with no headings. Cover: (1) what is genuinely moving, and "
    "whether it looks like a one-week spike or a sustained shift (use the streak column); (2) what is "
    "probably noise or too thin to trust (low levels, short history, weak sources such as YouTube "
    "region hints); (3) two follow-up questions worth investigating. Use ONLY numbers that appear in "
    "the brief. Never invent figures or events. Any explanation of why something is moving must be "
    "labelled as a hypothesis."
)


def narrate(brief_md, cfg):
    ncfg = cfg.get("narrative", {})
    if not ncfg.get("enabled", True) or not os.environ.get("ANTHROPIC_API_KEY"):
        return None
    try:
        import anthropic
    except ImportError:
        return None
    system = SYSTEM
    if ncfg.get("context"):
        system += " Context: " + ncfg["context"]
    client = anthropic.Anthropic()
    msg = client.messages.create(
        model=ncfg.get("model", "claude-sonnet-5-5"),
        max_tokens=900,
        system=system,
        messages=[{"role": "user", "content": brief_md}],
    )
    return "".join(b.text for b in msg.content if getattr(b, "type", "") == "text").strip()
