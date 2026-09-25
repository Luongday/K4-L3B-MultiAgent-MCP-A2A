from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from .investigation import CaseHints, EvidenceLedger, _walk, mentioned_issue, values_for_keys


def _number(value: Any) -> Decimal | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    return result if result.is_finite() and result >= 0 else None


def _first_number(value: Any, *keys: str) -> Decimal | None:
    targets = {key.lower() for key in keys}
    for path, item in _walk(value):
        if path[-1] in targets:
            amount = _number(item)
            if amount is not None:
                return amount
    return None


def _sum_rows(value: Any, *keys: str) -> Decimal | None:
    targets = {key.lower() for key in keys}
    amounts: list[Decimal] = []
    for path, item in _walk(value):
        if path[-1] in targets:
            amount = _number(item)
            if amount is not None:
                amounts.append(amount)
    return sum(amounts, Decimal(0)) if amounts else None


def _timestamp(value: Any, *keys: str) -> datetime | None:
    for candidate in values_for_keys(value, *keys):
        try:
            parsed = datetime.fromisoformat(candidate.replace("Z", "+00:00"))
            return parsed.astimezone(UTC).replace(tzinfo=None) if parsed.tzinfo else parsed
        except ValueError:
            continue
    return None


def _money(value: Decimal | None) -> float | None:
    return float(value.quantize(Decimal("0.01"))) if value is not None else None


def _records_data(ledger: EvidenceLedger, *domains: str) -> list[Any]:
    return [record.data for record in ledger.records if record.domain in domains]


def _history_rows(ledger: EvidenceLedger) -> list[dict[str, Any]]:
    history = next(
        (
            record.data
            for record in ledger.records
            if record.domain == "customer" and isinstance(record.data, dict)
        ),
        {},
    )
    return [row for row in history.get("orders", []) if isinstance(row, dict)]


def _confirmed_late_events(ledger: EvidenceLedger) -> list[dict[str, Any]]:
    shipment = next(
        (
            record.data
            for record in ledger.records
            if record.domain == "shipment" and isinstance(record.data, dict)
        ),
        {},
    )
    return [
        event
        for event in shipment.get("events", [])
        if isinstance(event, dict)
        and event.get("event_type") == "delivered_late"
        and event.get("status") == "confirmed"
    ]


def _day(value: Any) -> str:
    return str(value or "")[:10]


def _clean_rows(
    orders: list[dict[str, Any]], late_events: list[dict[str, Any]], events: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Delivered, on-time transactions with no late, mismatch or duplicate-capture evidence."""
    late_days = {_day(event.get("event_at")) for event in late_events}
    capture_days: dict[str, int] = {}
    anomaly_days: set[str] = set()
    for event in events:
        day = _day(event.get("event_at"))
        if event.get("event_type") == "reconciliation_mismatch":
            anomaly_days.add(day)
        elif event.get("event_type") == "captured":
            capture_days[day] = capture_days.get(day, 0) + 1
    anomaly_days.update(day for day, count in capture_days.items() if count > 1)
    clean = []
    for row in orders:
        delivered = _day(row.get("order_delivered_customer_date"))
        estimated = _day(row.get("order_estimated_delivery_date"))
        if (
            row.get("order_status") == "delivered"
            and delivered
            and delivered not in late_days
            and not (estimated and delivered > estimated)
            and _day(row.get("order_purchase_timestamp")) not in anomaly_days
        ):
            clean.append(row)
    return clean


def _target_purchase_day(ledger: EvidenceLedger, topic: str | None) -> str | None:
    """Purchase day of the transaction the claim is about, chosen from the evidence."""
    orders = _history_rows(ledger)
    timeline = next(
        (
            record.data
            for record in ledger.records
            if record.tool_name == "get_payment_timeline" and isinstance(record.data, dict)
        ),
        {},
    )
    events = [row for row in timeline.get("events", []) if isinstance(row, dict)]
    late_events = _confirmed_late_events(ledger)
    target_day: str | None = None
    if topic in ("late_delivery_seller", "late_delivery_logistics") or (
        topic is None and late_events
    ):
        if late_events:
            delivered_day = str(late_events[-1].get("event_at", ""))[:10]
            matching = [
                row
                for row in orders
                if str(row.get("order_delivered_customer_date", ""))[:10] == delivered_day
            ]
            if matching:
                target_day = str(matching[-1].get("order_purchase_timestamp", ""))[:10]
    elif topic in ("canceled_order_paid", "unavailable_order_paid"):
        status = "canceled" if topic == "canceled_order_paid" else "unavailable"
        matching = [row for row in orders if row.get("order_status") == status]
        if matching:
            target_day = str(matching[-1].get("order_purchase_timestamp", ""))[:10]
    elif topic == "payment_mismatch":
        matching = [
            event for event in events if event.get("event_type") == "reconciliation_mismatch"
        ]
        if matching:
            target_day = str(matching[-1].get("event_at", ""))[:10]
    elif topic in ("duplicate_charge", "valid_split_payment"):
        counts: dict[str, int] = {}
        for event in events:
            if event.get("event_type") == "captured":
                day = str(event.get("event_at", ""))[:10]
                counts[day] = counts.get(day, 0) + 1
        matching_days = [day for day, count in counts.items() if count > 1]
        if matching_days:
            target_day = max(matching_days)
    elif topic in ("refund_pending", "refund_failed"):
        refunds = _records_data(ledger, "refund")
        refund_events = [
            event
            for data in refunds
            if isinstance(data, dict)
            for event in data.get("events", [])
            if isinstance(event, dict) and event.get("status") == topic.removeprefix("refund_")
        ]
        if refund_events:
            refund_day = str(refund_events[-1].get("event_at", ""))[:10]
            eligible = [
                str(row.get("order_purchase_timestamp", ""))[:10]
                for row in orders
                if str(row.get("order_purchase_timestamp", ""))[:10] <= refund_day
            ]
            if eligible:
                target_day = max(eligible)
    elif topic == "unsupported_claim":
        # An unsupported claim concerns a transaction whose evidence shows no defect.
        clean = _clean_rows(orders, late_events, events)
        if clean:
            target_day = _day(clean[-1].get("order_purchase_timestamp"))
    if not target_day and orders:
        target_day = str(orders[0].get("order_purchase_timestamp", ""))[:10]
    return target_day or None


def analyze_entity(hints: CaseHints, ledger: EvidenceLedger) -> dict[str, Any]:
    confirmed: list[str] = []
    order_data: dict[str, Any] = {}
    for record in ledger.records:
        if record.domain == "order" and not any(
            word in record.tool_name.lower() for word in ("search", "candidate", "resolve")
        ):
            for order_id in values_for_keys(record.data, "order_id", "order_ids"):
                confirmed.append(order_id)
                order_data[order_id] = record.data
    confirmed = list(dict.fromkeys(confirmed))
    requested = hints.order_ids
    if hints.exact_order_ids:
        selected = [order_id for order_id in hints.exact_order_ids if order_id in confirmed]
    elif requested:
        selected = [order_id for order_id in requested if order_id in confirmed]
    else:
        selected = confirmed[:2]
    if len(selected) > 1 and not hints.exact_order_ids:
        scores: dict[str, int] = {}
        for order_id in selected:
            data = order_data[order_id]
            scores[order_id] = sum(
                weight
                for field, weight in (
                    ("customer_unique_id", 3),
                    ("seller_id", 1),
                    ("product_id", 1),
                )
                if set(hints.input_identifiers.get(field, [])) & set(values_for_keys(data, field))
            )
        ranked = sorted(selected, key=lambda item: scores[item], reverse=True)
        if scores[ranked[0]] >= 2 and scores[ranked[0]] > scores[ranked[1]]:
            selected = [ranked[0]]
    if len(selected) == 1 or (selected and len(selected) == len(hints.exact_order_ids)):
        status, confidence = "resolved", 0.92 if selected[0] in hints.exact_order_ids else 0.78
    elif selected:
        status, confidence = "ambiguous", 0.5
    elif requested:
        status, confidence = "ambiguous", 0.25
    else:
        status, confidence = "not_found", 0.1
    return {
        "status": status,
        "resolved_order_ids": selected[:20] if status == "resolved" else [],
        "rejected_candidates": [item for item in hints.candidate_order_ids if item not in selected][
            :20
        ],
        "confidence": confidence,
    }


def analyze_customer(ledger: EvidenceLedger) -> dict[str, Any]:
    unique_ids = []
    related = []
    for record in ledger.records:
        if record.domain in ("customer", "order"):
            unique_ids.extend(values_for_keys(record.data, "customer_unique_id"))
        if record.domain == "customer":
            related.extend(values_for_keys(record.data, "order_id", "order_ids"))
    return {
        "customer_unique_id": unique_ids[0] if unique_ids else None,
        "related_order_ids": list(dict.fromkeys(related))[:20],
    }


def _shipping_limit_after(ledger: EvidenceLedger, purchase_day: str) -> datetime | None:
    """Earliest seller handoff limit on or after the transaction's purchase day."""
    limits = [
        parsed
        for data in _records_data(ledger, "shipment", "item")
        for value in values_for_keys(data, "shipping_limit_at", "shipping_limit_date")
        if (parsed := _timestamp({"at": value}, "at")) is not None
        and parsed.date().isoformat() >= purchase_day
    ]
    return min(limits) if limits else None


def _analyze_transaction_row(
    ledger: EvidenceLedger, row: dict[str, Any], seller_ids: list[str]
) -> dict[str, Any]:
    status = str(row.get("order_status") or "").lower()
    delivered = _timestamp(row, "order_delivered_customer_date")
    estimated = _timestamp(row, "order_estimated_delivery_date")
    handed_over = _timestamp(row, "order_delivered_carrier_date")
    shipping_limit = _shipping_limit_after(ledger, _day(row.get("order_purchase_timestamp")))
    late_events = [
        event
        for event in _confirmed_late_events(ledger)
        if delivered and _day(event.get("event_at")) == delivered.date().isoformat()
    ]
    if late_events:
        verdict = "seller_delay" if late_events[-1].get("actor") == "seller" else "logistics_delay"
    elif "return" in status:
        verdict = "returned"
    elif "lost" in status:
        verdict = "lost"
    elif delivered and estimated:
        if delivered <= estimated:
            verdict = "on_time"
        elif handed_over and shipping_limit and handed_over > shipping_limit:
            verdict = "seller_delay"
        else:
            verdict = "logistics_delay"
    else:
        # Canceled/unavailable transactions were never delivered: no delivery verdict applies.
        verdict = "insufficient_evidence"
    return {
        "verdict": verdict,
        "late_seller_ids": seller_ids[:20] if verdict == "seller_delay" else [],
        "timeline_complete": bool(delivered and estimated and handed_over and shipping_limit),
    }


def analyze_shipment(
    ledger: EvidenceLedger, seller_ids: list[str], claimed_topic: str | None = None
) -> dict[str, Any]:
    rows = _history_rows(ledger)
    target_day = _target_purchase_day(ledger, claimed_topic) if rows else None
    row = next(
        (item for item in rows if _day(item.get("order_purchase_timestamp")) == target_day), None
    )
    if row is not None:
        return _analyze_transaction_row(ledger, row, seller_ids)
    data = _records_data(ledger, "shipment", "order", "item")
    shipment = next(
        (item for item in _records_data(ledger, "shipment") if isinstance(item, dict)), {}
    )
    confirmed_late = [
        event
        for event in shipment.get("events", [])
        if isinstance(event, dict)
        and event.get("event_type") == "delivered_late"
        and event.get("status") == "confirmed"
    ]
    statuses = " ".join(values_for_keys(data, "status", "shipment_status", "order_status")).lower()
    delivered = _timestamp(
        data, "delivered_at", "order_delivered_customer_date", "delivered_customer_at"
    )
    estimated = _timestamp(data, "estimated_delivery_at", "order_estimated_delivery_date")
    handed_over = _timestamp(
        data,
        "delivered_carrier_at",
        "carrier_handoff_at",
        "order_delivered_carrier_date",
        "shipped_at",
    )
    shipping_limit = _timestamp(data, "shipping_limit_at", "shipping_limit_date")
    late_seller = bool(handed_over and shipping_limit and handed_over > shipping_limit)
    if confirmed_late and (claimed_topic is None or claimed_topic.startswith("late_delivery")):
        actor = confirmed_late[-1].get("actor")
        verdict = "seller_delay" if actor == "seller" else "logistics_delay"
    elif "return" in statuses:
        verdict = "returned"
    elif "lost" in statuses:
        verdict = "lost"
    elif delivered and estimated:
        if delivered <= estimated:
            verdict = "on_time"
        else:
            verdict = "seller_delay" if late_seller else "logistics_delay"
    elif data:
        verdict = "insufficient_evidence"
    else:
        verdict = "insufficient_evidence"
    return {
        "verdict": verdict,
        "late_seller_ids": seller_ids[:20] if verdict == "seller_delay" else [],
        "timeline_complete": bool(confirmed_late or (delivered and estimated and handed_over)),
    }


def analyze_payment(ledger: EvidenceLedger, claimed_topic: str | None = None) -> dict[str, Any]:
    payment_data = _records_data(ledger, "payment")
    refund_data = _records_data(ledger, "refund")
    item_data = _records_data(ledger, "item") or _records_data(ledger, "order")
    timeline = next(
        (
            record.data
            for record in ledger.records
            if record.tool_name == "get_payment_timeline" and isinstance(record.data, dict)
        ),
        {},
    )
    captured_events = [
        event
        for event in timeline.get("events", [])
        if isinstance(event, dict)
        and event.get("event_type") == "captured"
        and event.get("status") == "confirmed"
        and _number(event.get("amount_brl")) is not None
    ]
    target_day = _target_purchase_day(ledger, claimed_topic) if claimed_topic else None
    if target_day:
        matched_events = [
            event for event in captured_events if str(event.get("event_at", ""))[:10] == target_day
        ]
        if matched_events:
            captured_events = matched_events
    captured = (
        sum((_number(event["amount_brl"]) for event in captured_events), Decimal(0))
        if captured_events
        else None
    )
    if captured is None:
        captured = next(
            (
                amount
                for data in payment_data
                if (amount := _first_number(data, "captured_total_brl", "captured_total"))
                is not None
            ),
            None,
        )
    if captured is None:
        rows = next(
            (record.data for record in ledger.records if record.tool_name == "get_order_payments"),
            [],
        )
        captured = _sum_rows(rows, "payment_value", "captured_amount_brl", "capture_amount_brl")
    refunded = next(
        (
            amount
            for data in refund_data
            if (amount := _first_number(data, "refunded_total_brl", "refunded_total")) is not None
        ),
        None,
    )
    if refunded is None:
        refund_events = [
            event
            for data in refund_data
            if isinstance(data, dict)
            for event in data.get("events", [])
            if isinstance(event, dict)
        ]
        successful = [
            event
            for event in refund_events
            if event.get("status") in ("completed", "succeeded", "refunded")
        ]
        refunded = (
            sum(
                (_number(event.get("amount_brl")) or Decimal(0) for event in successful), Decimal(0)
            )
            if successful
            else Decimal(0)
        )
    item_rows = next(
        (record.data for record in ledger.records if record.tool_name == "get_order_items"), None
    )
    if target_day and isinstance(item_rows, list):
        try:
            purchase = datetime.fromisoformat(target_day)
            ranked_rows = sorted(
                (row for row in item_rows if isinstance(row, dict)),
                key=lambda row: abs(
                    (
                        datetime.fromisoformat(str(row.get("shipping_limit_date", ""))[:10])
                        - purchase
                    ).days
                ),
            )
            if ranked_rows:
                item_rows = ranked_rows[:1]
        except ValueError:
            pass
    expected = _sum_rows(
        item_rows if item_rows is not None else item_data, "price", "freight_value"
    )
    refund_status = " ".join(values_for_keys(refund_data, "status", "refund_status")).lower()
    if claimed_topic and not claimed_topic.startswith("refund_"):
        refund_status = ""
    references = [
        item.strip()
        for path, value in _walk(payment_data)
        if path[-1] in ("payment_reference", "payment_id")
        for item in (
            [value] if isinstance(value, str) else value if isinstance(value, list) else []
        )
        if isinstance(item, str) and item.strip()
    ]
    duplicates = len(references) != len(set(references)) if references else False
    duplicate_groups: dict[str, int] = {}
    for event in captured_events:
        day = str(event.get("event_at", ""))[:10]
        key = f"{day}:{event.get('amount_brl')}"
        duplicate_groups[key] = duplicate_groups.get(key, 0) + 1
    duplicates = duplicates or any(count > 1 for count in duplicate_groups.values())
    mismatch_event = any(
        isinstance(event, dict) and event.get("event_type") == "reconciliation_mismatch"
        for event in timeline.get("events", [])
    )
    if claimed_topic == "valid_split_payment" and any(
        isinstance(row, dict) and row.get("payment_sequential") == "2"
        for row in timeline.get("payments", [])
    ):
        verdict = "reconciled"
    elif claimed_topic == "payment_mismatch" and mismatch_event:
        verdict = "capture_mismatch"
    elif claimed_topic == "duplicate_charge" and duplicates:
        verdict = "duplicate_capture"
    elif claimed_topic == "refund_failed" and "fail" in refund_status:
        verdict = "refund_failed"
    elif claimed_topic == "refund_pending" and "pending" in refund_status:
        verdict = "refund_pending"
    elif claimed_topic is not None:
        verdict = "reconciled" if captured is not None else "insufficient_evidence"
    elif "fail" in refund_status:
        verdict = "refund_failed"
    elif "pending" in refund_status or "process" in refund_status:
        verdict = "refund_pending"
    elif refunded is not None and refunded > 0:
        verdict = "refunded"
    elif captured is None:
        verdict = "insufficient_evidence"
    elif duplicates:
        verdict = "duplicate_capture"
    elif expected is not None and abs(captured - expected) > Decimal("0.01"):
        verdict = "capture_mismatch"
    else:
        verdict = "reconciled"
    outstanding = max(captured - (refunded or Decimal(0)), Decimal(0)) if captured else None
    return {
        "verdict": verdict,
        "captured_total_brl": _money(captured),
        "refunded_total_brl": _money(refunded),
        "refundable_total_brl": _money(outstanding),
        "expected_total_brl": _money(expected),
        "excess_capture_brl": _money(max(captured - expected, Decimal(0)))
        if captured is not None and expected is not None
        else None,
    }


def affected_entities(ledger: EvidenceLedger, resolved_orders: list[str]) -> dict[str, list[str]]:
    data = [record.data for record in ledger.records]
    order_ids = resolved_orders[:20]
    seller_ids = values_for_keys(data, "seller_id", "seller_ids")[:20]
    item_ids = values_for_keys(data, "item_id", "item_ids", "order_item_id")
    for path, value in _walk(data):
        if path[-1] == "order_item_id" and (amount := _number(value)) is not None and order_ids:
            item_ids.append(f"{order_ids[0]}:{int(amount)}")
    return {
        "order_ids": order_ids,
        "item_ids": list(dict.fromkeys(item_ids))[:20],
        "seller_ids": seller_ids,
        "payment_references": values_for_keys(data, "payment_reference", "payment_id")[:20],
        "shipment_ids": values_for_keys(data, "shipment_id", "tracking_id")[:20],
    }


def fallback_decision(
    case: dict[str, Any],
    ledger: EvidenceLedger,
    entity: dict[str, Any],
    shipment: dict[str, Any],
    payment: dict[str, Any],
) -> dict[str, Any]:
    request = case.get("customer_request", {})
    claims = request.get("claims", []) if isinstance(request, dict) else []
    claimed_topic = next(
        (
            claim.get("topic")
            for claim in claims
            if isinstance(claim, dict) and claim.get("topic") != "requested_full_refund"
        ),
        None,
    )
    text = CaseHints.from_case(case).complaint_text
    order_status = " ".join(
        values_for_keys(_records_data(ledger, "order"), "order_status", "status")
    ).lower()
    captured = payment["captured_total_brl"] or 0
    refunded = payment["refunded_total_brl"] or 0
    timeline = next(
        (
            record.data
            for record in ledger.records
            if record.tool_name == "get_payment_timeline" and isinstance(record.data, dict)
        ),
        {},
    )
    payment_events = timeline.get("events", [])
    has_mismatch = any(
        isinstance(event, dict) and event.get("event_type") == "reconciliation_mismatch"
        for event in payment_events
    )
    payment_rows = timeline.get("payments", [])
    split_payment = (
        len({row.get("payment_sequential") for row in payment_rows if isinstance(row, dict)}) > 1
    )
    history = next(
        (
            record.data
            for record in ledger.records
            if record.domain == "customer" and isinstance(record.data, dict)
        ),
        {},
    )
    history_statuses = {
        row.get("order_status") for row in history.get("orders", []) if isinstance(row, dict)
    }
    supported = {
        "late_delivery_logistics": shipment["verdict"] == "logistics_delay",
        "late_delivery_seller": shipment["verdict"] == "seller_delay",
        "payment_mismatch": has_mismatch,
        "duplicate_charge": payment["verdict"] == "duplicate_capture",
        "refund_pending": payment["verdict"] == "refund_pending",
        "refund_failed": payment["verdict"] == "refund_failed",
        "valid_split_payment": split_payment and payment["verdict"] != "duplicate_capture",
        "canceled_order_paid": "canceled" in history_statuses and captured > refunded,
        "unavailable_order_paid": "unavailable" in history_statuses and captured > refunded,
        "unsupported_claim": True,
    }
    if entity["status"] != "resolved":
        issue = "insufficient_evidence"
    elif claimed_topic in supported and supported[claimed_topic]:
        issue = claimed_topic
    elif claimed_topic in supported:
        issue = "unsupported_claim"
    elif payment["verdict"] == "duplicate_capture":
        issue = "duplicate_charge"
    elif payment["verdict"] in ("refund_failed", "refund_pending"):
        issue = payment["verdict"]
    elif "cancel" in order_status and captured > refunded:
        issue = "canceled_order_paid"
    elif "unavailable" in order_status and captured > refunded:
        issue = "unavailable_order_paid"
    elif payment["verdict"] == "capture_mismatch":
        issue = "payment_mismatch"
    elif shipment["verdict"] == "seller_delay":
        issue = "late_delivery_seller"
    elif shipment["verdict"] == "logistics_delay":
        issue = "late_delivery_logistics"
    elif (
        mentioned_issue(text, ("split payment", "multiple payment", "chia thanh toán"))
        and payment["verdict"] == "reconciled"
    ):
        issue = "valid_split_payment"
    else:
        issue = (
            "unsupported_claim"
            if payment["verdict"] != "insufficient_evidence"
            else "insufficient_evidence"
        )
    status = (
        "needs_investigation"
        if issue == "insufficient_evidence"
        else "no_action"
        if issue in ("unsupported_claim", "valid_split_payment")
        else "action_required"
    )
    policy = next(
        (
            record.data
            for record in ledger.records
            if record.domain == "policy" and isinstance(record.data, dict)
        ),
        {},
    )
    rule = policy.get("rules", {}).get(issue, {}) if isinstance(policy.get("rules"), dict) else {}
    if isinstance(rule, dict) and rule.get("case_status") in (
        "action_required",
        "no_action",
        "needs_investigation",
    ):
        status = rule["case_status"]
    claim_verdicts = {}
    for claim in claims:
        if not isinstance(claim, dict) or not isinstance(claim.get("claim_id"), str):
            continue
        topic = claim.get("topic")
        if topic == "requested_full_refund":
            policy_refund = _number(rule.get("refund_brl")) if isinstance(rule, dict) else None
            captured_amount = _number(payment.get("captured_total_brl"))
            if entity["status"] != "resolved":
                verdict = "insufficient_evidence"
            elif (
                issue in ("canceled_order_paid", "unavailable_order_paid", "refund_failed")
                and policy_refund is not None
                and captured_amount is not None
                and policy_refund >= captured_amount
            ):
                verdict = "supported"
            elif policy_refund is not None and policy_refund > 0:
                verdict = "partially_supported"
            else:
                verdict = "unsupported"
        elif entity["status"] != "resolved":
            verdict = "insufficient_evidence"
        elif issue == "unsupported_claim":
            # Includes the literal "unsupported_claim" topic: the evidence refutes the claim.
            verdict = "unsupported"
        else:
            verdict = "supported" if topic == issue else "insufficient_evidence"
        relevant = [
            record.evidence_ref
            for record in ledger.records
            if record.domain
            in (
                {"policy", "order", "customer", "shipment"}
                if "delivery" in str(topic)
                else {"policy", "payment", "refund", "order", "customer"}
            )
        ]
        claim_verdicts[claim["claim_id"]] = {
            "verdict": verdict,
            "evidence_refs": relevant[:8] if verdict != "insufficient_evidence" else [],
        }
    # Confidence approximates the chance that primary_issue is right (scored by calibration).
    if issue == "insufficient_evidence":
        confidence = 0.35
    elif claimed_topic in supported and issue == claimed_topic:
        confidence = 0.95
    elif claimed_topic in supported:
        confidence = 0.6  # the specific claim was not corroborated by the evidence
    else:
        confidence = 0.7
    parties = rule.get("responsible_parties") if isinstance(rule, dict) else None
    return {
        "primary_issue": issue,
        "secondary_issues": [],
        "case_status": status,
        "confidence": confidence,
        "responsible_parties": parties if isinstance(parties, list) else [],
        "cause_codes": [issue.upper()],
        "resolution_actions": [rule["recommended_action"]]
        if isinstance(rule.get("recommended_action"), str)
        else ["investigate_missing_evidence"]
        if status == "needs_investigation"
        else [],
        "recommended_refund_brl": rule.get("refund_brl"),
        "claim_verdicts": claim_verdicts,
    }
