from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

from .mcp_gateway import EvidenceGateway, ToolCallError, ToolSpec

MAX_MCP_CALLS = 16
DOMAIN_ORDER = (
    "item",
    "payment",
    "shipment",
    "refund",
    "customer",
    "policy",
    "seller",
    "product",
)
# Order-scoped domains each claim topic needs beyond order, customer history and policy.
# Item and product context are always in the investigation scope of the L3B inputs.
TOPIC_DOMAINS: dict[str, tuple[str, ...]] = {
    "late_delivery_logistics": ("item", "payment", "shipment", "product"),
    "late_delivery_seller": ("item", "payment", "shipment", "seller", "product"),
    "valid_split_payment": ("item", "payment", "product"),
    "payment_mismatch": ("item", "payment", "product"),
    "duplicate_charge": ("item", "payment", "product"),
    "refund_pending": ("item", "payment", "refund", "product"),
    "refund_failed": ("item", "payment", "refund", "product"),
    # Canceled/unavailable orders carry no refund lifecycle; the refund tool only errors there.
    "canceled_order_paid": ("item", "payment", "product"),
    "unavailable_order_paid": ("item", "payment", "seller", "product"),
    "unsupported_claim": ("item", "payment", "shipment", "product"),
}
DOMAIN_WORDS = {
    "order": ("order", "purchase"),
    "customer": ("customer", "buyer", "history"),
    "item": ("item",),
    "shipment": ("shipment", "shipping", "delivery", "tracking"),
    "payment": ("payment", "capture", "charge"),
    "refund": ("refund",),
    "seller": ("seller", "merchant"),
    "product": ("product", "catalog"),
    "policy": ("policy", "rule"),
}
IDENTIFIER_FIELDS = {
    "order_id": ("order_id", "order_ids"),
    "customer_unique_id": ("customer_unique_id", "customer_unique_id_hint"),
    "customer_id": ("customer_id",),
    "seller_id": ("seller_id", "seller_ids"),
    "product_id": ("product_id", "product_ids"),
    "payment_reference": ("payment_reference", "payment_references", "payment_id"),
    "shipment_id": ("shipment_id", "shipment_ids", "tracking_id"),
    "policy_version": ("policy_version",),
}


def _walk(value: Any, parents: tuple[str, ...] = ()) -> list[tuple[tuple[str, ...], Any]]:
    found: list[tuple[tuple[str, ...], Any]] = []
    if isinstance(value, dict):
        for key, item in value.items():
            path = (*parents, str(key).lower())
            found.append((path, item))
            found.extend(_walk(item, path))
    elif isinstance(value, list):
        for item in value:
            found.extend(_walk(item, parents))
    return found


def _strings(value: Any) -> list[str]:
    if isinstance(value, str) and value.strip():
        return [value.strip()]
    if isinstance(value, list):
        return [item.strip() for item in value if isinstance(item, str) and item.strip()]
    return []


def values_for_keys(value: Any, *keys: str) -> list[str]:
    targets = {key.lower() for key in keys}
    results: list[str] = []
    for path, item in _walk(value):
        if path[-1] in targets:
            results.extend(_strings(item))
    return list(dict.fromkeys(results))


def claimed_topic(case: dict[str, Any]) -> str | None:
    """Return the customer's issue claim, ignoring the generic full-refund request."""
    request = case.get("customer_request", {})
    claims = request.get("claims", []) if isinstance(request, dict) else []
    return next(
        (
            claim.get("topic")
            for claim in claims
            if isinstance(claim, dict)
            and isinstance(claim.get("topic"), str)
            and claim.get("topic") != "requested_full_refund"
        ),
        None,
    )


@dataclass
class CaseHints:
    exact_order_ids: list[str] = field(default_factory=list)
    candidate_order_ids: list[str] = field(default_factory=list)
    claimed_order_ids: list[str] = field(default_factory=list)
    identifiers: dict[str, list[str]] = field(default_factory=dict)
    input_identifiers: dict[str, list[str]] = field(default_factory=dict)
    complaint_text: str = ""

    @classmethod
    def from_case(cls, case: dict[str, Any]) -> CaseHints:
        hints = cls()
        for path, item in _walk(case):
            key = path[-1]
            if "order" in key and ("id" in key or "candidate" in key):
                target = (
                    hints.candidate_order_ids
                    if any(
                        "candidate" in part or "possible" in part or "claimed" in part
                        for part in path
                    )
                    else hints.exact_order_ids
                )
                target.extend(_strings(item))
                if "claimed" in key:
                    hints.claimed_order_ids.extend(_strings(item))
            if key == "text" or any(
                word in key
                for word in ("complaint", "message", "description", "claim", "narrative")
            ):
                hints.complaint_text += " ".join(_strings(item)) + " "
        for canonical, aliases in IDENTIFIER_FIELDS.items():
            hints.identifiers[canonical] = values_for_keys(case, *aliases)
            hints.input_identifiers[canonical] = hints.identifiers[canonical].copy()
        hints.exact_order_ids = list(dict.fromkeys(hints.exact_order_ids))[:5]
        # The customer's claimed order is checked first; other candidates follow input order.
        hints.candidate_order_ids = [
            item
            for item in dict.fromkeys([*hints.claimed_order_ids, *hints.candidate_order_ids])
            if item not in hints.exact_order_ids
        ][:5]
        hints.complaint_text = hints.complaint_text.strip()[:2000]
        return hints

    @property
    def order_ids(self) -> list[str]:
        return list(dict.fromkeys([*self.exact_order_ids, *self.candidate_order_ids]))

    def absorb(self, data: Any, *, order_candidates: bool = False) -> None:
        for canonical, aliases in IDENTIFIER_FIELDS.items():
            existing = self.identifiers.setdefault(canonical, [])
            existing.extend(
                item for item in values_for_keys(data, *aliases) if item not in existing
            )
        if order_candidates:
            for order_id in values_for_keys(data, "order_id", "order_ids"):
                if order_id not in self.order_ids:
                    self.candidate_order_ids.append(order_id)
            self.candidate_order_ids = self.candidate_order_ids[:5]


@dataclass(frozen=True)
class EvidenceRecord:
    case_id: str
    tool_name: str
    domain: str
    evidence_ref: str
    data: Any


@dataclass
class EvidenceLedger:
    case_id: str
    records: list[EvidenceRecord] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    _cache: dict[str, EvidenceRecord | None] = field(default_factory=dict)
    call_count: int = 0
    consecutive_errors: int = 0

    async def call(
        self, gateway: EvidenceGateway, tool: ToolSpec, arguments: dict[str, Any]
    ) -> EvidenceRecord | None:
        cache_key = json.dumps([tool.name, arguments], sort_keys=True, ensure_ascii=False)
        if cache_key in self._cache:
            return self._cache[cache_key]
        if self.call_count >= MAX_MCP_CALLS:
            self.errors.append("MCP_CALL_BUDGET_EXHAUSTED")
            return None
        self.call_count += 1
        try:
            response = await gateway.call(tool.name, case_id=self.case_id, **arguments)
            record = EvidenceRecord(
                case_id=self.case_id,
                tool_name=tool.name,
                domain=response["domain"],
                evidence_ref=response["evidence_ref"],
                data=response["data"],
            )
        # Transport failures (GatewayUnavailable) propagate so the whole case is retried
        # on a fresh session instead of being finalized with silently missing evidence.
        except (ToolCallError, ValueError, KeyError) as exc:
            self.errors.append(f"{tool.name}: {type(exc).__name__}")
            self._cache[cache_key] = None
            self.consecutive_errors += 1
            if self.consecutive_errors >= 3 and not self.records:
                raise RuntimeError(
                    f"MCP failed on three tools before returning evidence for {self.case_id}"
                ) from exc
            return None
        self.consecutive_errors = 0
        if record.evidence_ref not in {item.evidence_ref for item in self.records}:
            self.records.append(record)
        self._cache[cache_key] = record
        return record


def _tool_domain(tool: ToolSpec) -> str | None:
    name = tool.name.lower()
    named = [
        (name.rfind(word), domain)
        for domain, words in DOMAIN_WORDS.items()
        for word in words
        if word in name
    ]
    if named:
        return max(named)[1]
    description = tool.description.lower()
    described = [
        (len(word), domain)
        for domain, words in DOMAIN_WORDS.items()
        for word in words
        if word in description
    ]
    return max(described)[1] if described else None


def _arg_value(key: str, hints: CaseHints, order_id: str | None) -> Any:
    normalized = key.lower()
    if normalized == "order_id":
        return order_id
    if normalized == "order_ids":
        return [order_id] if order_id else hints.order_ids
    if normalized in {"query", "search_query", "search_term"}:
        return order_id or next(
            (
                values[0]
                for name, values in hints.identifiers.items()
                if name != "order_id" and values
            ),
            None,
        )
    for canonical, aliases in IDENTIFIER_FIELDS.items():
        if normalized in aliases or normalized == canonical:
            matches = hints.identifiers.get(canonical, [])
            return matches[0] if matches else None
    return None


def _build_args(tool: ToolSpec, hints: CaseHints, order_id: str | None) -> dict[str, Any] | None:
    schema = tool.input_schema if isinstance(tool.input_schema, dict) else {}
    properties = schema.get("properties", {})
    if not isinstance(properties, dict):
        return None
    required = set(schema.get("required", [])) - {"case_id"}
    args: dict[str, Any] = {}
    for key in properties:
        if key == "case_id":
            continue
        value = _arg_value(key, hints, order_id)
        if value is not None and value != []:
            args[key] = value
    if not required.issubset(args):
        return None
    # Calls using only case_id are allowed only for tools explicitly scoped to one case.
    if not args and not any(word in tool.name.lower() for word in ("case", "overview")):
        return None
    return args


def _pick_tool(tools: tuple[ToolSpec, ...], domain: str) -> ToolSpec | None:
    """Choose one tool per domain; a payment timeline carries both rows and lifecycle events."""
    matches = [tool for tool in tools if _tool_domain(tool) == domain]
    matches.sort(
        key=lambda tool: (
            domain == "payment" and "timeline" not in tool.name.lower(),
            "get" not in tool.name.lower(),
            tool.name,
        )
    )
    return matches[0] if matches else None


def _is_search(tool_name: str) -> bool:
    return any(word in tool_name.lower() for word in ("search", "candidate", "resolve"))


async def collect_evidence(
    case: dict[str, Any], gateway: EvidenceGateway
) -> tuple[EvidenceLedger, CaseHints]:
    case_id = case["case_id"]
    hints = CaseHints.from_case(case)
    ledger = EvidenceLedger(case_id)
    tools = await gateway.discover_tools()
    topic = claimed_topic(case)
    history_order_ids: set[str] = set()
    customer_tool = _pick_tool(tools, "customer")
    if customer_tool and hints.identifiers.get("customer_unique_id"):
        args = _build_args(customer_tool, hints, None)
        if args is not None:
            record = await ledger.call(gateway, customer_tool, args)
            if record:
                history_order_ids.update(values_for_keys(record.data, "order_id", "order_ids"))
                hints.absorb(record.data, order_candidates=not hints.order_ids)
    if not hints.order_ids:
        search_tools = [
            tool
            for tool in tools
            if any(word in tool.name.lower() for word in ("search", "candidate", "resolve", "case"))
        ]
        for tool in search_tools[:2]:
            args = _build_args(tool, hints, None)
            if args is None:
                continue
            record = await ledger.call(gateway, tool, args)
            if record:
                hints.absorb(record.data, order_candidates=True)
    order_tools = [
        tool for tool in tools if _tool_domain(tool) == "order" and not _is_search(tool.name)
    ]
    order_tools.sort(key=lambda tool: ("get_order" not in tool.name.lower(), tool.name))
    for tool in order_tools[:1]:
        for order_id in hints.order_ids[:5]:
            args = _build_args(tool, hints, order_id)
            if args is None:
                continue
            record = await ledger.call(gateway, tool, args)
            if record:
                hints.absorb(record.data)
                # An order confirmed by get_order and by the customer's own history is
                # independently verified; remaining candidates are rejected without a call.
                if order_id in history_order_ids and order_id in values_for_keys(
                    record.data, "order_id"
                ):
                    break
            if ledger.call_count >= MAX_MCP_CALLS:
                break
    confirmed: dict[str, EvidenceRecord] = {}
    for record in ledger.records:
        if record.domain == "order" and not _is_search(record.tool_name):
            for order_id in values_for_keys(record.data, "order_id"):
                confirmed[order_id] = record
    customer_ids = values_for_keys(case, "customer_unique_id", "customer_unique_id_hint")
    customer_matches = [
        order_id
        for order_id, record in confirmed.items()
        if customer_ids
        and set(values_for_keys(record.data, "customer_unique_id")) & set(customer_ids)
    ]
    if hints.exact_order_ids:
        active_order_ids = [item for item in hints.exact_order_ids if item in confirmed][:2]
    elif customer_matches:
        active_order_ids = customer_matches[:2]
    else:
        active_order_ids = list(confirmed)[:2]
    if not active_order_ids:
        # Without a confirmed order, order-scoped evidence would describe an unverified entity.
        return ledger, hints
    domains = TOPIC_DOMAINS.get(topic or "", DOMAIN_ORDER)
    for domain in ("policy", *domains):
        tool = _pick_tool(tools, domain)
        if tool is None or (domain == "customer" and customer_tool is not None):
            continue
        order_ids = (
            active_order_ids if "order_id" in tool.input_schema.get("properties", {}) else [None]
        )
        for order_id in order_ids:
            args = _build_args(tool, hints, order_id)
            if args is None:
                continue
            await ledger.call(gateway, tool, args)
            if ledger.call_count >= MAX_MCP_CALLS:
                return ledger, hints
    return ledger, hints


def mentioned_issue(text: str, words: tuple[str, ...]) -> bool:
    normalized = re.sub(r"\s+", " ", text.lower())
    return any(word in normalized for word in words)
