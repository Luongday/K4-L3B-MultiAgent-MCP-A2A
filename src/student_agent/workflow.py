from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Any

from .a2a import A2AProtocol
from .analysis import (
    _target_purchase_day,
    affected_entities,
    analyze_customer,
    analyze_entity,
    analyze_payment,
    analyze_shipment,
    fallback_decision,
)
from .investigation import EvidenceLedger, EvidenceRecord, claimed_topic, collect_evidence
from .mcp_gateway import EvidenceGateway
from .trace import TraceWriter

ACTION_ISSUES = {
    "canceled_order_paid",
    "unavailable_order_paid",
    "late_delivery_seller",
    "late_delivery_logistics",
    "payment_mismatch",
    "duplicate_charge",
    "refund_pending",
    "refund_failed",
}


def _safe_list(value: Any, limit: int, max_length: int = 80) -> list[str]:
    if not isinstance(value, list):
        return []
    return list(
        dict.fromkeys(
            item.strip()[:max_length] for item in value if isinstance(item, str) and item.strip()
        )
    )[:limit]


def _safe_money(value: Any) -> Decimal | None:
    if isinstance(value, bool):
        return None
    try:
        amount = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    return amount.quantize(Decimal("0.01")) if amount.is_finite() and amount >= 0 else None


def _responsible_parties(
    issue: str, sellers: list[str], policy_parties: list[Any] | None = None
) -> list[dict[str, Any]]:
    # Policy names the responsible party type; seller identities come from this case's
    # evidence because the policy is shared across cases.
    types = [
        party["party_type"]
        for party in policy_parties or []
        if isinstance(party, dict) and isinstance(party.get("party_type"), str)
    ]
    if types:
        parties: list[dict[str, Any]] = []
        for party_type in dict.fromkeys(types):
            if party_type == "seller":
                parties.extend({"party_type": "seller", "party_id": seller} for seller in sellers)
            else:
                parties.append({"party_type": party_type, "party_id": None})
        if parties:
            return parties[:5]
    if issue == "late_delivery_seller" and sellers:
        return [{"party_type": "seller", "party_id": seller} for seller in sellers[:5]]
    if issue == "late_delivery_logistics":
        return [{"party_type": "logistics_provider", "party_id": None}]
    if issue == "unavailable_order_paid" and sellers:
        return [{"party_type": "seller", "party_id": sellers[0]}]
    if issue in ("duplicate_charge", "payment_mismatch", "refund_failed", "refund_pending"):
        return [{"party_type": "payment_provider", "party_id": None}]
    if issue == "canceled_order_paid":
        return [{"party_type": "platform", "party_id": None}]
    if issue in ("valid_split_payment", "unsupported_claim"):
        return [{"party_type": "customer", "party_id": None}]
    return [{"party_type": "unknown", "party_id": None}]


def _financial_resolution(decision: dict[str, Any], findings: dict[str, Any]) -> dict[str, Any]:
    issue = decision["primary_issue"]
    payment = findings["payment_analysis"]
    outstanding = _safe_money(payment["refundable_total_brl"])
    proposed = _safe_money(decision["recommended_refund_brl"])
    if (
        outstanding is None
        or issue not in ACTION_ISSUES
        or decision["case_status"] != "action_required"
        or issue == "refund_pending"
        or (
            issue in ("late_delivery_seller", "late_delivery_logistics")
            and not findings["policy_available"]
        )
    ):
        amount = Decimal(0)
    elif proposed is not None:
        amount = min(proposed, outstanding)
    elif issue in ("canceled_order_paid", "unavailable_order_paid"):
        amount = outstanding
    else:
        amount = Decimal(0)
    amount_float = float(amount)
    orders = findings["entity_resolution"]["resolved_order_ids"]
    lines = (
        [
            {
                "reason_code": issue.upper(),
                "amount_brl": amount_float,
                "entity_id": orders[0] if orders else None,
            }
        ]
        if amount > 0
        else []
    )
    return {"currency": "BRL", "recommended_refund_brl": amount_float, "refund_lines": lines}


def _conflicts(ledger: EvidenceLedger, claimed_topic: str | None = None) -> list[dict[str, Any]]:
    conflicts: list[dict[str, Any]] = []
    order = next((item for item in ledger.records if item.tool_name == "get_order"), None)
    history = next(
        (item for item in ledger.records if item.tool_name == "get_customer_history"), None
    )
    if order and history and isinstance(order.data, dict) and isinstance(history.data, dict):
        rows = [
            row
            for row in history.data.get("orders", [])
            if isinstance(row, dict) and row.get("order_id") == order.data.get("order_id")
        ]
        target_day = _target_purchase_day(ledger, claimed_topic)
        target_index = next(
            (
                index
                for index, row in enumerate(rows)
                if str(row.get("order_purchase_timestamp", ""))[:10] == target_day
            ),
            None,
        )
        order_source = f"get_order:{order.evidence_ref}"[:80]
        history_sources = [
            f"get_customer_history:{history.evidence_ref}:row{index}"[:80]
            for index in range(len(rows))
        ]
        for field in (
            "order_status",
            "order_purchase_timestamp",
            "order_delivered_carrier_date",
            "order_delivered_customer_date",
            "order_estimated_delivery_date",
        ):
            values = [
                str(value)
                for value in [order.data.get(field), *(row.get(field) for row in rows)]
                if value is not None
            ]
            if len(set(values)) < 2:
                continue
            selected = history_sources[target_index] if target_index is not None else None
            if target_index is not None and rows[target_index].get(field) == order.data.get(field):
                selected = order_source
            conflicts.append(
                {
                    "field": field,
                    "sources": [order_source, *history_sources][:5],
                    "selected_source": selected,
                    "resolution_code": "MATCHED_CLAIM_TIMELINE"
                    if selected
                    else "UNRESOLVED_CONFLICT",
                }
            )
    return conflicts[:5]


def _claim_assessments(
    case: dict[str, Any], decision: dict[str, Any], refs: list[str]
) -> list[dict[str, Any]]:
    request = case.get("customer_request", {})
    claims = case.get("claims") or (request.get("claims") if isinstance(request, dict) else None)
    if not isinstance(claims, list):
        return []
    verdicts = decision.get("claim_verdicts", {})
    if not isinstance(verdicts, dict):
        verdicts = {}
    result = []
    for claim in claims[:5]:
        if not isinstance(claim, dict) or not isinstance(claim.get("claim_id"), str):
            continue
        claim_id = claim["claim_id"][:64]
        claim_decision = verdicts.get(claim_id, {})
        if isinstance(claim_decision, str):
            claim_decision = {"verdict": claim_decision}
        if not isinstance(claim_decision, dict):
            claim_decision = {}
        verdict = claim_decision.get("verdict", "insufficient_evidence")
        if verdict not in (
            "supported",
            "unsupported",
            "partially_supported",
            "insufficient_evidence",
        ):
            verdict = "insufficient_evidence"
        chosen_refs = _safe_list(claim_decision.get("evidence_refs"), 30, 96)
        chosen_refs = [ref for ref in chosen_refs if ref in refs]
        if verdict != "insufficient_evidence" and not chosen_refs:
            verdict = "insufficient_evidence"
        result.append(
            {
                "claim_id": claim_id,
                "verdict": verdict,
                "confidence": decision["confidence"] if verdict != "insufficient_evidence" else 0.3,
                "evidence_refs": chosen_refs if verdict != "insufficient_evidence" else [],
            }
        )
    return result


def _verify(output: dict[str, Any], ledger: EvidenceLedger, trace: TraceWriter) -> None:
    trace.contracts.validate_output(output, f"outputs/{ledger.case_id}.json")
    if output["case_id"] != ledger.case_id:
        raise ValueError("case_id changed during investigation")
    known_refs = {record.evidence_ref for record in ledger.records}
    submitted_refs = set(output["evidence_refs"])
    if not submitted_refs.issubset(known_refs):
        raise ValueError("output contains an unknown evidence ref")
    for claim in output.get("claim_assessments", []):
        if not set(claim["evidence_refs"]).issubset(submitted_refs):
            raise ValueError("claim references evidence outside the output")
    entity = output["entity_resolution"]
    if set(entity["resolved_order_ids"]) & set(entity["rejected_candidates"]):
        raise ValueError("resolved order is also rejected")
    if output["shipment_analysis"]["late_seller_ids"] and not set(
        output["shipment_analysis"]["late_seller_ids"]
    ).issubset(output["affected_entities"]["seller_ids"]):
        raise ValueError("late seller is absent from affected_entities")
    financial = output["financial_resolution"]
    total = sum(Decimal(str(item["amount_brl"])) for item in financial["refund_lines"])
    if abs(total - Decimal(str(financial["recommended_refund_brl"]))) > Decimal("0.01"):
        raise ValueError("refund lines do not match recommended refund")
    refundable = output["payment_analysis"]["refundable_total_brl"]
    if refundable is not None and financial["recommended_refund_brl"] > refundable + 0.01:
        raise ValueError("recommended refund exceeds refundable amount")


def _evidence_for(ledger: EvidenceLedger, *domains: str) -> list[EvidenceRecord]:
    return [record for record in ledger.records if record.domain in domains]


async def solve_case(
    case: dict[str, Any],
    gateway: EvidenceGateway,
    trace: TraceWriter,
) -> dict[str, Any]:
    case_id = case["case_id"]
    a2a = A2AProtocol(case_id, trace)
    evidence_task = a2a.assign("evidence-agent", {"case_id": case_id})
    ledger, hints = await collect_evidence(case, gateway)
    if not ledger.records:
        raise RuntimeError(f"MCP returned no usable evidence for {case_id}")
    evidence_result = a2a.complete(evidence_task, {"ledger": ledger, "hints": hints})
    ledger = evidence_result.payload["ledger"]
    hints = evidence_result.payload["hints"]

    entity_task = a2a.assign("entity-agent", {"hints": hints})
    entity = analyze_entity(hints, ledger)
    customer = analyze_customer(ledger)
    entity_result = a2a.complete(
        entity_task,
        {"entity_resolution": entity, "customer_context": customer},
        _evidence_for(ledger, "order", "customer"),
    )
    entity = entity_result.payload["entity_resolution"]
    customer = entity_result.payload["customer_context"]

    order_task = a2a.assign("order-agent", {"resolved_order_ids": entity["resolved_order_ids"]})
    entities = affected_entities(ledger, entity["resolved_order_ids"])
    order_result = a2a.complete(
        order_task,
        {"affected_entities": entities},
        _evidence_for(ledger, "order", "item", "seller", "product"),
    )
    entities = order_result.payload["affected_entities"]

    topic = claimed_topic(case)
    shipment_task = a2a.assign("shipment-agent", {"seller_ids": entities["seller_ids"]})
    shipment = analyze_shipment(ledger, entities["seller_ids"], topic)
    shipment_result = a2a.complete(
        shipment_task,
        {"shipment_analysis": shipment},
        _evidence_for(ledger, "shipment", "order", "customer", "item"),
    )
    shipment = shipment_result.payload["shipment_analysis"]

    payment_task = a2a.assign("payment-agent", {"order_ids": entity["resolved_order_ids"]})
    payment = analyze_payment(ledger, topic)
    payment_result = a2a.complete(
        payment_task,
        {"payment_analysis": payment},
        _evidence_for(ledger, "payment", "refund", "order", "item"),
    )
    payment = payment_result.payload["payment_analysis"]

    findings = {
        "entity_resolution": entity,
        "customer_context": customer,
        "shipment_analysis": shipment,
        "payment_analysis": payment,
        "affected_entities": entities,
        "policy_available": any(record.domain == "policy" for record in ledger.records),
    }
    policy_task = a2a.assign("policy-agent", findings)
    decision = fallback_decision(case, ledger, entity, shipment, payment)
    trace.emit(
        case_id=case_id,
        event_type="policy_decided",
        actor="policy-agent",
        decision_code=decision["primary_issue"].upper(),
    )
    policy_result = a2a.complete(
        policy_task,
        {"decision": decision},
        _evidence_for(ledger, "policy"),
    )
    decision = policy_result.payload["decision"]
    refs = [record.evidence_ref for record in ledger.records][:30]
    conflict_task = a2a.assign("conflict-agent", {"evidence_refs": refs})
    conflicts = _conflicts(ledger, topic)
    conflict_result = a2a.complete(
        conflict_task,
        {"data_conflicts": conflicts},
        _evidence_for(ledger, "order", "customer"),
    )
    conflicts = conflict_result.payload["data_conflicts"]
    if any(item["selected_source"] is None for item in conflicts):
        decision["confidence"] = min(decision["confidence"], 0.6)
    output: dict[str, Any] = {
        "schema_version": "day09-l3b-output-v2",
        "case_id": case_id,
        "assessment": {
            "primary_issue": decision["primary_issue"],
            "secondary_issues": decision["secondary_issues"],
            "case_status": decision["case_status"],
            "confidence": decision["confidence"],
        },
        "affected_entities": entities,
        "entity_resolution": entity,
        "customer_context": customer,
        "shipment_analysis": shipment,
        "payment_analysis": {
            key: payment[key]
            for key in (
                "verdict",
                "captured_total_brl",
                "refunded_total_brl",
                "refundable_total_brl",
            )
        },
        "root_cause_analysis": {
            "ranked_causes": [
                {"cause_code": code, "rank": rank}
                for rank, code in enumerate(decision["cause_codes"][:5], 1)
            ],
            "responsible_parties": _responsible_parties(
                decision["primary_issue"],
                shipment["late_seller_ids"] or entities["seller_ids"],
                decision.get("responsible_parties"),
            ),
        },
        "evidence_refs": refs,
        "data_conflicts": conflicts,
        "financial_resolution": _financial_resolution(decision, findings),
        "resolution_actions": decision["resolution_actions"],
    }
    claims = _claim_assessments(case, decision, refs)
    if claims:
        output["claim_assessments"] = claims
    verifier_task = a2a.assign("verifier", {"case_id": case_id})
    _verify(output, ledger, trace)
    trace.emit(
        case_id=case_id, event_type="verification_completed", actor="verifier", decision_code="PASS"
    )
    a2a.complete(verifier_task, {"verified": True})
    a2a.assert_complete()
    return output
