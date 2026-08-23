"""A deliberately tiny Markdown subset: `**bold**` and `*emphasis*`, nothing else.

The section writer marks the numbers and named entities it wants emphasized,
and the print template renders them larger and in the brand colour. That needs
*some* markup in the body text, and every richer option is worse here:

- Raw HTML from the model means turning off Jinja's autoescaping for
  model-authored text, which is an injection hole.
- A full Markdown library brings link and image syntax, so a writer could put
  a URL into a document whose design deliberately carries none.

So: escape everything first, then re-introduce exactly two tags. Nothing the
model writes can produce any other markup, because by the time the converter
runs there are no live angle brackets left to find.

`strip()` is the other half of the contract. Word limits are a promise about
what a reader sees, and the anchor/filler/hedge gates in the generation graph
are all about real prose — so those run on stripped text while only the
renderer sees the marked-up form.
"""

import html
import re

from markupsafe import Markup

# Bold first: `**x**` would otherwise be eaten as emphasis wrapping `*x*`.
_BOLD_RE = re.compile(r"\*\*(.+?)\*\*", re.DOTALL)
# A single `*` not adjacent to another `*`, so bold markers never re-match.
_EM_RE = re.compile(r"(?<!\*)\*(?!\*)(.+?)(?<!\*)\*(?!\*)", re.DOTALL)


def to_html(text: str) -> Markup:
    """Escape, then convert the two supported markers. Safe to render unescaped."""
    out = html.escape(text)
    out = _BOLD_RE.sub(r"<strong>\1</strong>", out)
    out = _EM_RE.sub(r"<em>\1</em>", out)
    return Markup(out)


def strip(text: str) -> str:
    """The plain prose a reader sees — what every word count and content gate
    should measure."""
    out = _BOLD_RE.sub(r"\1", text)
    return _EM_RE.sub(r"\1", out)
