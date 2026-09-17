"""Filter + rank: hard requirements AND together; absence counts as unknown.

Same semantics as examples/pick_model.py check + rank. A fact that is ABSENT
can never *satisfy* a requirement, and an unknown price sorts last (price 0 is
treated as unknown — free-plan mirrors don't expose real pricing).
"""

from __future__ import annotations


def check(
    rec: dict,
    *,
    min_context: int | None = None,
    max_input_price: float | None = None,
    max_output_price: float | None = None,
    tool_call: bool = False,
    reasoning: bool = False,
    json_output: bool = False,
    image_input: bool = False,
    provider: str | None = None,
) -> list[str]:
    """Return the list of failed requirements (empty = the record qualifies)."""
    failed: list[str] = []
    if min_context is not None and (rec.get("context_window") or 0) < min_context:
        failed.append(f"context>={min_context}")
    for want, field in ((tool_call, "tool_call"), (reasoning, "reasoning"), (json_output, "structured_output")):
        if want and rec.get(field) is not True:
            failed.append(field)
    if image_input and "image" not in (rec.get("modalities") or {}).get("input", []):
        failed.append("image-input")
    if provider is not None and rec.get("provider") != provider:
        failed.append(f"provider={provider}")
    cost = rec.get("cost") or {}
    if max_input_price is not None:
        p = cost.get("input_per_mtok")
        if p is None or p > max_input_price:
            failed.append(f"input-price<={max_input_price}")
    if max_output_price is not None:
        p = cost.get("output_per_mtok")
        if p is None or p > max_output_price:
            failed.append(f"output-price<={max_output_price}")
    return failed


def rank(entries: list[tuple[str, dict]]) -> list[tuple[str, dict]]:
    """Cheapest output first; unknown price last (0 == unknown); bigger context wins ties."""
    def key(item: tuple[str, dict]):
        _, rec = item
        out = (rec.get("cost") or {}).get("output_per_mtok")
        if not out:
            out = None
        return (out is None, out or 0.0, -(rec.get("context_window") or 0))

    return sorted(entries, key=key)


def find(
    table: dict,
    *,
    min_context: int | None = None,
    max_input_price: float | None = None,
    max_output_price: float | None = None,
    tool_call: bool = False,
    reasoning: bool = False,
    json_output: bool = False,
    image_input: bool = False,
    provider: str | None = None,
    limit: int | None = 10,
) -> list[tuple[str, dict]]:
    """Filter the table by hard requirements and rank what survives.

    Returns [(key, rec), ...] — cheapest output price first, unknown prices
    last, ties broken by larger context window. Every filter ANDs; an absent
    fact never qualifies.
    """
    kwargs = dict(
        min_context=min_context,
        max_input_price=max_input_price,
        max_output_price=max_output_price,
        tool_call=tool_call,
        reasoning=reasoning,
        json_output=json_output,
        image_input=image_input,
        provider=provider,
    )
    entries = [(k, r) for k, r in table["models"].items() if not check(r, **kwargs)]
    entries = rank(entries)
    return entries if limit is None else entries[:limit]
