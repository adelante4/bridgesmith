"""Derive a working print palette from two auto-detected brand colours.

The detected values are whatever a vision pass found on a company's cover, and
they are not a design system. Acme's came back primary `#FF4B00`, accent
`#000000`; `brochure_v1` interpolated those two straight into a full-page
gradient and produced an orange-to-black A4 bleed. A different sender could
just as easily return two colours that vibrate against each other, or a primary
too pale to read as text.

So nothing here trusts the inputs as a pair. The page is always near-white
paper with near-black ink; `primary` supplies one hue at several strengths; and
`accent` is used as a genuine second hue only when it earns it — chromatic
enough to read as a colour, far enough from primary in hue to look intentional,
and legible on paper. When it doesn't qualify (Acme's black, or a near-white),
it is demoted to ink and hairline duty rather than discarded, which is exactly
what Acme's own material does with black.
"""

import colorsys
from dataclasses import dataclass

PAPER = "#FAFAF8"
INK = "#1A1A1A"

# Below this saturation a colour reads as a neutral, not a hue.
_MIN_CHROMA = 0.18
# Two hues closer than this look like a mistake rather than a pairing.
_MIN_HUE_DISTANCE = 0.08
# WCAG AA for body text; also the bar for a colour used on small type.
_MIN_TEXT_CONTRAST = 4.5


def _to_rgb(hex_color: str) -> tuple[float, float, float]:
    h = hex_color.lstrip("#")
    if len(h) == 3:
        h = "".join(c * 2 for c in h)
    return tuple(int(h[i : i + 2], 16) / 255 for i in (0, 2, 4))


def _to_hex(rgb: tuple[float, float, float]) -> str:
    return "#" + "".join(f"{max(0, min(255, round(c * 255))):02x}" for c in rgb)


def _relative_luminance(rgb: tuple[float, float, float]) -> float:
    def channel(c: float) -> float:
        return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4

    r, g, b = (channel(c) for c in rgb)
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def contrast_ratio(a: str, b: str) -> float:
    la, lb = _relative_luminance(_to_rgb(a)), _relative_luminance(_to_rgb(b))
    lighter, darker = max(la, lb), min(la, lb)
    return (lighter + 0.05) / (darker + 0.05)


def _mix_with_paper(hex_color: str, strength: float) -> str:
    """A tint of the brand colour on paper. Mixing toward the actual paper
    colour rather than pure white keeps panels on the same warm axis as the
    page instead of appearing as a cold patch."""
    brand, paper = _to_rgb(hex_color), _to_rgb(PAPER)
    return _to_hex(tuple(p + (b - p) * strength for b, p in zip(brand, paper)))


def _darken_until_legible(hex_color: str) -> str:
    """Walk lightness down until the colour is safe for small type on paper.
    A pale brand colour is fine as a rule or a panel fill and unreadable as a
    word, so text gets its own variant rather than the page losing the hue."""
    r, g, b = _to_rgb(hex_color)
    h, lightness, s = colorsys.rgb_to_hls(r, g, b)
    candidate = hex_color
    while lightness > 0.05 and contrast_ratio(candidate, PAPER) < _MIN_TEXT_CONTRAST:
        lightness -= 0.04
        candidate = _to_hex(colorsys.hls_to_rgb(h, lightness, s))
    return candidate


def _is_chromatic(hex_color: str) -> bool:
    r, g, b = _to_rgb(hex_color)
    _, lightness, s = colorsys.rgb_to_hls(r, g, b)
    return s >= _MIN_CHROMA and 0.06 < lightness < 0.94


def _hue_distance(a: str, b: str) -> float:
    ha = colorsys.rgb_to_hls(*_to_rgb(a))[0]
    hb = colorsys.rgb_to_hls(*_to_rgb(b))[0]
    d = abs(ha - hb) % 1.0
    return min(d, 1.0 - d)


@dataclass(frozen=True)
class Palette:
    paper: str
    ink: str
    brand: str  # full-strength primary — marks, emphasis, large type
    brand_text: str  # contrast-guarded primary — safe for small type
    brand_tint: str  # panel fills
    brand_rule: str  # hairlines and dividers
    accent: str  # second hue, or ink when the detected accent didn't qualify
    accent_text: str  # contrast-guarded accent — safe for small type
    accent_is_hue: bool

    def as_css_vars(self) -> dict[str, str]:
        return {
            "paper": self.paper,
            "ink": self.ink,
            "brand": self.brand,
            "brand_text": self.brand_text,
            "brand_tint": self.brand_tint,
            "brand_rule": self.brand_rule,
            "accent": self.accent,
            "accent_text": self.accent_text,
        }


def derive(primary: str, accent: str | None = None) -> Palette:
    """Build the print palette. `primary` carries the identity; `accent` is
    promoted to a second hue only when it is chromatic, distinct from primary,
    and legible — otherwise it becomes the ink and rule colour."""
    # Qualification deliberately does NOT require text contrast. An accent's
    # job here is rules, marks and tile edges, and a warm hue like amber is a
    # perfectly good hairline while scoring ~1.7 against paper. Gating on text
    # contrast threw away most warm brand accents; type gets `accent_text`
    # instead, which is darkened until it is actually readable.
    accent_is_hue = bool(
        accent
        and _is_chromatic(accent)
        and _is_chromatic(primary)
        and _hue_distance(primary, accent) >= _MIN_HUE_DISTANCE
    )

    # Only an ACHROMATIC accent becomes ink. Acme's black is the ink of their own
    # material, so adopting it keeps the page on-brand. A chromatic accent that
    # merely sits too close to primary in hue must not become ink — that would
    # set the whole body text in, say, blue for a navy/blue brand pair.
    if accent and not _is_chromatic(accent) and contrast_ratio(accent, PAPER) >= _MIN_TEXT_CONTRAST:
        ink = accent
    else:
        ink = INK

    return Palette(
        paper=PAPER,
        ink=ink,
        brand=primary,
        brand_text=_darken_until_legible(primary),
        brand_tint=_mix_with_paper(primary, 0.06),
        brand_rule=_mix_with_paper(primary, 0.20),
        accent=accent if accent_is_hue else ink,
        accent_text=_darken_until_legible(accent) if accent_is_hue else ink,
        accent_is_hue=accent_is_hue,
    )
