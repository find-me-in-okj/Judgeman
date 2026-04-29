"""
output.py — Terminal output primitives for Judgeman.

Design decisions:
- No rich dependency. Uses click.echo + colorama for portability.
- Every output function has a clear semantic purpose (header, field, warning,
  error, section). The CLI commands call these, not raw print().
- Colors are used sparingly and with meaning:
    - Cyan: entity IDs, labels, structure
    - Green: success states, addressed items
    - Yellow: warnings, ceilings, incomplete items
    - Red: errors, violations, hard blocks
    - No color: body text — confidence scores, descriptions, rationale
- The analyst's words (statements, rationale, what_if_wrong) are NEVER
  colorized. Color belongs to the system's metadata, not the analyst's reasoning.
"""

import click
import textwrap
from colorama import Fore, Style, init as colorama_init

colorama_init(autoreset=True)

W = 72  # output width


def _c(color: str, text: str) -> str:
    return f"{color}{text}{Style.RESET_ALL}"


def cyan(t):   return _c(Fore.CYAN, t)
def green(t):  return _c(Fore.GREEN, t)
def yellow(t): return _c(Fore.YELLOW, t)
def red(t):    return _c(Fore.RED, t)
def bold(t):   return _c(Style.BRIGHT, t)
def dim(t):    return _c(Style.DIM, t)


def rule(char="─"):
    click.echo(dim(char * W))


def header(title: str, subtitle: str = ""):
    click.echo()
    rule("═")
    click.echo(bold(f"  {title}"))
    if subtitle:
        click.echo(dim(f"  {subtitle}"))
    rule("═")


def section(title: str):
    click.echo()
    click.echo(cyan(f"▸ {title}"))
    rule()


def field(label: str, value: str, indent: int = 2, color=None):
    """Print a label: value pair. Value is never colorized by default."""
    label_str = cyan(f"{label}:")
    pad = " " * indent
    val = color(str(value)) if color else str(value)
    click.echo(f"{pad}{label_str} {val}")


def multiline_field(label: str, value: str, indent: int = 2):
    """Print a label followed by wrapped body text on the next line."""
    click.echo(f"{'  ' * (indent // 2)}{cyan(label + ':')}")
    wrapped = textwrap.fill(
        value, width=W - indent - 2,
        initial_indent=" " * (indent + 2),
        subsequent_indent=" " * (indent + 2),
    )
    click.echo(wrapped)


def success(msg: str):
    click.echo(green(f"  ✓ {msg}"))


def warn(msg: str):
    click.echo(yellow(f"  ⚠  {msg}"))


def error(msg: str):
    click.echo(red(f"  ✗ {msg}"))


def info(msg: str):
    click.echo(dim(f"  · {msg}"))


def entity_id(label: str, id_str: str):
    """Print a newly-created entity ID. Analyst needs to copy this."""
    click.echo(f"  {cyan(label + ':')} {bold(id_str)}")


def confidence_bar(value: float, ceiling: float, width: int = 30) -> str:
    """
    Visual representation of confidence with ceiling marker.
    Example: [████████████░░░░░░|░░░░░░░░░░] 60%  ceiling: 75%
    """
    filled = int(value * width)
    ceil_pos = int(ceiling * width)
    bar = ""
    for i in range(width):
        if i < filled:
            bar += "█"
        elif i == ceil_pos and ceil_pos < width:
            bar += cyan("|")
        else:
            bar += "░"
    pct = f"{value:.0%}"
    ceil_pct = f"{ceiling:.0%}"
    color = green if value >= 0.70 else yellow if value >= 0.40 else red
    return f"[{color(bar)}] {bold(pct)}  {dim('ceiling: ' + ceil_pct)}"


def confidence_breakdown(bd):
    """Print the full ConfidenceBreakdown in analyst-readable form."""
    from confidence import ConfidenceBreakdown

    section("Confidence breakdown")
    field("Claim", bd.claim_statement[:70] + ("…" if len(bd.claim_statement) > 70 else ""))
    field("Impact level", bd.impact_level,
          color=red if bd.impact_level == "high" else yellow if bd.impact_level == "medium" else None)
    click.echo()

    # Base confidence
    click.echo(f"  {cyan('Base confidence:')}  {bd.base_confidence:.0%}  {dim('(analyst-assigned)')}")

    # Factors
    for f in bd.factors:
        sign = "+" if f.value > 0 else ""
        val_str = f"{sign}{f.value:.3f}"
        color = green if f.value > 0 else red if f.value < 0 else dim
        label = f.name.replace("_", " ")
        click.echo(f"  {color(val_str):>12}  {cyan(label)}")
        wrapped = textwrap.fill(
            f.explanation, width=W - 16,
            initial_indent=" " * 16,
            subsequent_indent=" " * 16,
        )
        click.echo(dim(wrapped))

    rule()
    click.echo(f"  {cyan('Raw score:')}     {bd.raw_confidence:.0%}")

    if bd.ceiling_applied:
        click.echo(yellow(f"  Ceiling applied at {bd.ceiling:.0%} (impact: {bd.impact_level})"))

    click.echo()
    click.echo(f"  {confidence_bar(bd.displayed_confidence(), bd.ceiling)}")

    if bd.autolift and bd.autolift.applicable:
        click.echo()
        if bd.autolift.criteria_met:
            click.echo(green(f"  ✦ AUTO-LIFT ACTIVE — ceiling raised {IMPACT_CEILINGS[bd.impact_level]:.0%} → {bd.ceiling:.0%}"))
            for m in bd.autolift.met_criteria():
                click.echo(green(f"    ✓ {m}"))
        else:
            click.echo(cyan(f"  ◈ AUTO-LIFT LOCKED — {len(bd.autolift.unmet_criteria())} criterion/a remaining"))
            for m in bd.autolift.met_criteria():
                click.echo(green(f"    ✓ {m}"))
            for u in bd.autolift.unmet_criteria():
                click.echo(yellow(f"    · {u}"))

    if bd.override_active:
        click.echo()
        warn(f"OVERRIDE ACTIVE: analyst-forced confidence {bd.override_confidence:.0%}")
        if bd.override_justification:
            click.echo(dim(f"    Justification: {bd.override_justification}"))

    if not bd.high_impact_requirements_met:
        click.echo()
        warn("High-impact safety requirements incomplete:")
        if not bd.has_counter_claim:
            click.echo(red("    ✗ No counter-claim registered"))
        if not bd.has_what_if_wrong:
            click.echo(red("    ✗ 'What if I'm wrong?' not provided"))

    if bd.improvement_paths:
        click.echo()
        click.echo(cyan("  What would increase confidence:"))
        for p in bd.improvement_paths:
            click.echo(dim(f"    + {p}"))

    if bd.reduction_risks:
        click.echo()
        click.echo(cyan("  What could decrease confidence:"))
        for r in bd.reduction_risks:
            click.echo(dim(f"    - {r}"))


def prompt_required(prompt: str, **kwargs) -> str:
    """Prompt with required input validation (non-empty)."""
    while True:
        val = click.prompt(prompt, **kwargs).strip()
        if val:
            return val
        click.echo(red("  Value cannot be empty."))


def prompt_float(prompt: str, min_val: float = 0.0, max_val: float = 1.0) -> float:
    """Prompt for a float in [min_val, max_val]."""
    while True:
        raw = click.prompt(prompt).strip()
        try:
            val = float(raw)
            if min_val <= val <= max_val:
                return val
            click.echo(red(f"  Must be between {min_val} and {max_val}."))
        except ValueError:
            click.echo(red("  Must be a number (e.g. 0.75)."))


def prompt_choice(prompt: str, choices: list[str]) -> str:
    """Prompt for a value from a fixed set of choices."""
    choices_str = " / ".join(choices)
    while True:
        val = click.prompt(f"{prompt} [{choices_str}]").strip().lower()
        if val in choices:
            return val
        click.echo(red(f"  Must be one of: {choices_str}"))
