from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

from student_agent.a2a import A2AProtocol
from student_agent.analysis import analyze_entity, analyze_payment, analyze_shipment
from student_agent.contracts import Contracts
from student_agent.investigation import CaseHints, EvidenceLedger, EvidenceRecord, _tool_domain
from student_agent.mcp_gateway import GatewayUnavailable, ToolCallError, ToolSpec
from student_agent.trace import TraceWriter
from student_agent.workflow import _conflicts, _financial_resolution, solve_case

ROOT = Path(__file__).resolve().parents[1]
REFS = {
    "order": "ev_order_012345678901234567890123",
    "shipment": "ev_shipment_0123456789012345678901",
    "payment": "ev_payment_01234567890123456789012",
}


class FakeGateway:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, dict[str, Any]]] = []
        self.tools = (
            ToolSpec(
                "get_order",
                "Get order",
                {
                    "properties": {"case_id": {}, "order_id": {}},
                    "required": ["case_id", "order_id"],
                },
            ),
            ToolSpec(
                "get_shipment",
                "Get shipment",
                {
                    "properties": {"case_id": {}, "order_id": {}},
                    "required": ["case_id", "order_id"],
                },
            ),
            ToolSpec(
                "get_payment",
                "Get payment",
                {
                    "properties": {"case_id": {}, "order_id": {}},
                    "required": ["case_id", "order_id"],
                },
            ),
        )

    async def discover_tools(self) -> tuple[ToolSpec, ...]:
        return self.tools

    async def call(self, name: str, *, case_id: str, **arguments: Any) -> dict[str, Any]:
        self.calls.append((name, case_id, arguments))
        data = {
            "get_order": {
                "order_id": "ORDER_001",
                "seller_id": "SELLER_001",
                "customer_unique_id": "CUSTOMER_001",
                "items": [{"price": 90, "freight_value": 10}],
                "shipping_limit_at": "2018-03-15T10:00:00",
            },
            "get_shipment": {
                "order_id": "ORDER_001",
                "shipment_status": "delivered",
                "carrier_handoff_at": "2018-03-16T10:00:00",
                "delivered_at": "2018-03-20T10:00:00",
                "estimated_delivery_at": "2018-03-19T10:00:00",
            },
            "get_payment": {"order_id": "ORDER_001", "captured_total_brl": 100},
        }[name]
        domain = name.removeprefix("get_")
        return {"domain": domain, "data": data, "evidence_ref": REFS[domain]}


class CandidateGateway:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.tools = (
            ToolSpec(
                "search_orders",
                "Search order candidates by customer",
                {
                    "properties": {"case_id": {}, "customer_unique_id": {}},
                    "required": ["case_id", "customer_unique_id"],
                },
            ),
            ToolSpec(
                "get_order",
                "Get one order",
                {
                    "properties": {"case_id": {}, "order_id": {}},
                    "required": ["case_id", "order_id"],
                },
            ),
        )

    async def discover_tools(self) -> tuple[ToolSpec, ...]:
        return self.tools

    async def call(self, name: str, *, case_id: str, **arguments: Any) -> dict[str, Any]:
        assert case_id == "CASE_002"
        self.calls.append((name, arguments))
        if name == "search_orders":
            return {
                "domain": "order",
                "evidence_ref": "ev_search_01234567890123456789012",
                "data": {"order_ids": ["ORDER_001", "ORDER_002"]},
            }
        if arguments["order_id"] == "ORDER_002":
            raise ToolCallError("order not found")
        return {
            "domain": "order",
            "evidence_ref": "ev_order_012345678901234567890123",
            "data": {"order_id": "ORDER_001", "customer_unique_id": "CUSTOMER_001"},
        }


def test_workflow_uses_discovered_tools_and_emits_real_evidence_trace(tmp_path: Path) -> None:
    gateway = FakeGateway()
    trace_path = tmp_path / "trace.jsonl"
    trace = TraceWriter(trace_path, Contracts(ROOT / "contracts" / "schemas"))
    case = {"case_id": "CASE_001", "order_id": "ORDER_001", "complaint": "Delivery was late"}

    trace.emit(case_id=case["case_id"], event_type="case_received", actor="coordinator")
    output = asyncio.run(solve_case(case, gateway, trace))
    trace.emit(case_id=case["case_id"], event_type="case_finalized", actor="coordinator")

    assert output["entity_resolution"]["status"] == "resolved"
    assert output["assessment"]["primary_issue"] == "late_delivery_seller"
    assert output["payment_analysis"]["captured_total_brl"] == 100.0
    assert set(output["evidence_refs"]) == set(REFS.values())
    assert len(gateway.calls) == 3
    assert all(call[1] == "CASE_001" for call in gateway.calls)
    events = [json.loads(line) for line in trace_path.read_text(encoding="utf-8").splitlines()]
    assert events[0]["event_type"] == "case_received"
    assert events[-1]["event_type"] == "case_finalized"
    assert {event["event_type"] for event in events} >= {
        "task_assigned",
        "handoff",
        "tool_result_consumed",
        "verification_completed",
    }
    consumed = {
        ref
        for event in events
        if event["event_type"] == "tool_result_consumed"
        for ref in event["evidence_refs"]
    }
    assert consumed == set(output["evidence_refs"])


def test_candidate_search_requires_detail_evidence_before_resolution(tmp_path: Path) -> None:
    gateway = CandidateGateway()
    trace = TraceWriter(tmp_path / "trace.jsonl", Contracts(ROOT / "contracts" / "schemas"))
    case = {"case_id": "CASE_002", "customer_unique_id": "CUSTOMER_001"}

    output = asyncio.run(solve_case(case, gateway, trace))

    assert output["entity_resolution"]["resolved_order_ids"] == ["ORDER_001"]
    assert output["entity_resolution"]["rejected_candidates"] == ["ORDER_002"]
    assert gateway.calls[0] == ("search_orders", {"customer_unique_id": "CUSTOMER_001"})
    assert ("get_order", {"order_id": "ORDER_001"}) in gateway.calls
    assert ("get_order", {"order_id": "ORDER_002"}) in gateway.calls


def test_a2a_rejects_cross_case_evidence_and_repeated_handoff(tmp_path: Path) -> None:
    trace = TraceWriter(tmp_path / "trace.jsonl", Contracts(ROOT / "contracts" / "schemas"))
    protocol = A2AProtocol("CASE_004", trace)
    task = protocol.assign("entity-agent")
    foreign = EvidenceRecord("CASE_999", "get_order", "order", REFS["order"], {})

    with pytest.raises(ValueError, match="cross-case"):
        protocol.complete(task, {}, [foreign])
    result = protocol.complete(task, {"status": "done"})
    assert result.case_id == "CASE_004"
    with pytest.raises(ValueError, match="already completed"):
        protocol.complete(task, {})


def test_live_tool_names_route_to_their_specific_domains() -> None:
    assert _tool_domain(ToolSpec("get_order_items", "item and seller rows", {})) == "item"
    assert _tool_domain(ToolSpec("get_order_payments", "payment lifecycle", {})) == "payment"
    assert _tool_domain(ToolSpec("get_shipment_summary", "seller limits", {})) == "shipment"


def test_customer_identity_breaks_order_candidate_tie() -> None:
    hints = CaseHints.from_case(
        {
            "case_id": "CASE_005",
            "customer_unique_id": "CUSTOMER_001",
            "candidate_order_ids": ["ORDER_001", "ORDER_002"],
        }
    )
    ledger = EvidenceLedger("CASE_005")
    ledger.records = [
        EvidenceRecord(
            "CASE_005",
            "get_order",
            "order",
            REFS["order"],
            {"order_id": "ORDER_001", "customer_unique_id": "CUSTOMER_001"},
        ),
        EvidenceRecord(
            "CASE_005",
            "get_order",
            "order",
            "ev_order_222222222222222222222222",
            {"order_id": "ORDER_002", "customer_unique_id": "CUSTOMER_999"},
        ),
    ]

    result = analyze_entity(hints, ledger)

    assert result["status"] == "resolved"
    assert result["resolved_order_ids"] == ["ORDER_001"]
    assert result["rejected_candidates"] == ["ORDER_002"]


def test_payment_uses_transaction_day_selected_by_confirmed_delivery() -> None:
    ledger = EvidenceLedger("CASE_006")
    ledger.records = [
        EvidenceRecord(
            "CASE_006",
            "get_order",
            "order",
            "ev_order",
            {
                "order_id": "ORDER_006",
                "order_purchase_timestamp": "2018-05-11T09:00:00-03:00",
                "order_delivered_customer_date": "2018-05-20T09:00:00-03:00",
            },
        ),
        EvidenceRecord(
            "CASE_006",
            "get_customer_history",
            "customer",
            "ev_history",
            {
                "orders": [
                    {
                        "order_id": "ORDER_006",
                        "order_purchase_timestamp": "2018-05-11T09:00:00-03:00",
                        "order_delivered_customer_date": "2018-05-20T09:00:00-03:00",
                    },
                    {
                        "order_id": "ORDER_006",
                        "order_purchase_timestamp": "2017-12-20T09:00:00-03:00",
                        "order_delivered_customer_date": "2018-01-04T09:00:00-03:00",
                    },
                ]
            },
        ),
        EvidenceRecord(
            "CASE_006",
            "get_shipment_summary",
            "shipment",
            "ev_shipment",
            {
                "events": [
                    {
                        "event_at": "2018-01-04T09:00:00-03:00",
                        "event_type": "delivered_late",
                        "actor": "logistics_provider",
                        "status": "confirmed",
                    }
                ]
            },
        ),
        EvidenceRecord(
            "CASE_006",
            "get_payment_timeline",
            "payment",
            "ev_payment",
            {
                "events": [
                    {
                        "event_at": "2018-05-11T10:00:00-03:00",
                        "event_type": "captured",
                        "amount_brl": "89.00",
                        "status": "confirmed",
                    },
                    {
                        "event_at": "2017-12-20T10:00:00-03:00",
                        "event_type": "captured",
                        "amount_brl": "16.00",
                        "status": "confirmed",
                    },
                ]
            },
        ),
    ]

    assert analyze_shipment(ledger, ["seller-1"])["verdict"] == "logistics_delay"
    assert (
        analyze_shipment(ledger, ["seller-1"], "unsupported_claim")["verdict"]
        == "insufficient_evidence"
    )
    assert analyze_payment(ledger, "late_delivery_logistics")["captured_total_brl"] == 16.0
    conflicts = _conflicts(ledger, "late_delivery_logistics")
    assert {item["field"] for item in conflicts} >= {"order_delivered_customer_date"}
    assert all(item["resolution_code"] == "MATCHED_CLAIM_TIMELINE" for item in conflicts)


def test_payment_mismatch_does_not_mix_historical_capture() -> None:
    ledger = EvidenceLedger("CASE_007")
    ledger.records = [
        EvidenceRecord(
            "CASE_007",
            "get_payment_timeline",
            "payment",
            "ev_payment",
            {
                "events": [
                    {
                        "event_at": "2018-01-07T10:00:00-03:00",
                        "event_type": "captured",
                        "amount_brl": "35.00",
                        "status": "confirmed",
                    },
                    {
                        "event_at": "2018-01-07T12:00:00-03:00",
                        "event_type": "reconciliation_mismatch",
                        "status": "open",
                    },
                    {
                        "event_at": "2018-04-23T10:00:00-03:00",
                        "event_type": "captured",
                        "amount_brl": "89.00",
                        "status": "confirmed",
                    },
                ]
            },
        ),
    ]

    result = analyze_payment(ledger, "payment_mismatch")
    assert result["verdict"] == "capture_mismatch"
    assert result["captured_total_brl"] == 35.0


def test_duplicate_refund_uses_policy_amount_within_captured_balance() -> None:
    result = _financial_resolution(
        {
            "primary_issue": "duplicate_charge",
            "case_status": "action_required",
            "recommended_refund_brl": 64,
        },
        {
            "payment_analysis": {"refundable_total_brl": 128, "excess_capture_brl": 39},
            "entity_resolution": {"resolved_order_ids": ["ORDER_001"]},
            "policy_available": True,
        },
    )
    assert result["recommended_refund_brl"] == 64.0
    assert result["refund_lines"][0]["amount_brl"] == 64.0


def _live_tool(name: str, argument: str = "order_id") -> ToolSpec:
    return ToolSpec(
        name,
        "",
        {"properties": {"case_id": {}, argument: {}}, "required": ["case_id", argument]},
    )


LIVE_TOOLS = (
    _live_tool("get_customer_history", "customer_unique_id"),
    _live_tool("get_order"),
    _live_tool("get_order_items"),
    _live_tool("get_order_payments"),
    _live_tool("get_payment_timeline"),
    _live_tool("get_policy", "policy_version"),
    _live_tool("get_product_context"),
    _live_tool("get_refund_timeline"),
    _live_tool("get_sellers"),
    _live_tool("get_shipment_summary"),
)
LIVE_DOMAINS = {
    "get_customer_history": "customer",
    "get_order": "order",
    "get_order_items": "item",
    "get_payment_timeline": "payment",
    "get_policy": "policy",
    "get_product_context": "product",
}


class LiveShapedGateway:
    def __init__(self, data: dict[str, Any]) -> None:
        self.data = data
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def discover_tools(self) -> tuple[ToolSpec, ...]:
        return LIVE_TOOLS

    async def call(self, name: str, *, case_id: str, **arguments: Any) -> dict[str, Any]:
        self.calls.append((name, arguments))
        if arguments.get("order_id", "ORDER_010") != "ORDER_010" or name not in self.data:
            raise ToolCallError(f"MCP tool {name} failed")
        return {
            "domain": LIVE_DOMAINS[name],
            "evidence_ref": f"ev_{name}_0123456789abcdef",
            "data": self.data[name],
        }


def test_verified_claimed_order_skips_fake_candidate_and_follows_topic_plan(
    tmp_path: Path,
) -> None:
    row = {
        "order_id": "ORDER_010",
        "order_status": "delivered",
        "order_purchase_timestamp": "2018-02-19T09:00:00-03:00",
        "order_delivered_carrier_date": "2018-02-21T09:00:00-03:00",
        "order_delivered_customer_date": "2018-02-28T09:00:00-03:00",
        "order_estimated_delivery_date": "2018-03-01T09:00:00-03:00",
    }
    gateway = LiveShapedGateway(
        {
            "get_customer_history": {"customer_unique_id": "customer-10", "orders": [row]},
            "get_order": row,
            "get_order_items": [
                {
                    "order_id": "ORDER_010",
                    "order_item_id": "item-10",
                    "seller_id": "seller-10",
                    "shipping_limit_date": "2018-02-22T09:00:00-03:00",
                    "price": "79.00",
                    "freight_value": "10.00",
                }
            ],
            "get_payment_timeline": {
                "events": [
                    {
                        "event_at": "2018-02-19T10:00:00-03:00",
                        "event_type": "captured",
                        "amount_brl": "35.00",
                        "status": "confirmed",
                    },
                    {
                        "event_at": "2018-02-19T12:00:00-03:00",
                        "event_type": "reconciliation_mismatch",
                        "status": "open",
                    },
                ]
            },
            "get_policy": {
                "rules": {
                    "payment_mismatch": {
                        "case_status": "action_required",
                        "recommended_action": "reconcile_payment",
                        "refund_brl": 35.0,
                        "responsible_parties": [{"party_type": "payment_provider"}],
                    }
                }
            },
            "get_product_context": [{"product_id": "product-10"}],
        }
    )
    case = {
        "case_id": "L3B_CASE_010",
        "customer_request": {
            "claimed_order_id": "ORDER_010",
            "claims": [
                {"claim_id": "claim-a", "topic": "payment_mismatch"},
                {"claim_id": "claim-b", "topic": "requested_full_refund"},
            ],
        },
        "policy_version": "EC_POLICY_V2",
        "candidate_order_ids": ["ORDER_010", "candidate-010"],
        "customer_unique_id_hint": "customer-10",
    }
    trace = TraceWriter(tmp_path / "trace.jsonl", Contracts(ROOT / "contracts" / "schemas"))

    output = asyncio.run(solve_case(case, gateway, trace))

    called = [name for name, _ in gateway.calls]
    assert called.count("get_order") == 1
    assert not {"get_order_payments", "get_shipment_summary", "get_sellers"} & set(called)
    assert output["entity_resolution"]["resolved_order_ids"] == ["ORDER_010"]
    assert output["entity_resolution"]["rejected_candidates"] == ["candidate-010"]
    assert output["assessment"]["primary_issue"] == "payment_mismatch"
    assert output["financial_resolution"]["recommended_refund_brl"] == 35.0
    assert output["root_cause_analysis"]["responsible_parties"] == [
        {"party_type": "payment_provider", "party_id": None}
    ]


def _history_ledger(
    case_id: str, rows: list[dict[str, Any]], extra: list[EvidenceRecord]
) -> EvidenceLedger:
    ledger = EvidenceLedger(case_id)
    ledger.records = [
        EvidenceRecord(case_id, "get_customer_history", "customer", "ev_history", {"orders": rows}),
        *extra,
    ]
    return ledger


def test_unsupported_claim_targets_the_defect_free_transaction() -> None:
    late_row = {
        "order_status": "delivered",
        "order_purchase_timestamp": "2018-08-05T09:00:00-03:00",
        "order_delivered_carrier_date": "2018-08-07T09:00:00-03:00",
        "order_delivered_customer_date": "2018-08-20T09:00:00-03:00",
        "order_estimated_delivery_date": "2018-08-15T09:00:00-03:00",
    }
    clean_row = {
        "order_status": "delivered",
        "order_purchase_timestamp": "2018-06-25T09:00:00-03:00",
        "order_delivered_carrier_date": "2018-06-27T09:00:00-03:00",
        "order_delivered_customer_date": "2018-07-04T09:00:00-03:00",
        "order_estimated_delivery_date": "2018-07-05T09:00:00-03:00",
    }
    shipment = EvidenceRecord(
        "CASE_007",
        "get_shipment_summary",
        "shipment",
        "ev_shipment",
        {
            "shipping_limits": [
                {"shipping_limit_at": "2018-08-08T09:00:00-03:00"},
                {"shipping_limit_at": "2018-06-28T09:00:00-03:00"},
            ],
            "events": [
                {
                    "event_at": "2018-08-20T09:00:00-03:00",
                    "event_type": "delivered_late",
                    "actor": "logistics_provider",
                    "status": "confirmed",
                }
            ],
        },
    )
    payment = EvidenceRecord(
        "CASE_007",
        "get_payment_timeline",
        "payment",
        "ev_payment",
        {
            "events": [
                {
                    "event_at": "2018-08-05T10:00:00-03:00",
                    "event_type": "captured",
                    "amount_brl": "16.00",
                    "status": "confirmed",
                },
                {
                    "event_at": "2018-06-25T10:00:00-03:00",
                    "event_type": "captured",
                    "amount_brl": "89.00",
                    "status": "confirmed",
                },
            ]
        },
    )
    ledger = _history_ledger("CASE_007", [late_row, clean_row], [shipment, payment])

    shipment_result = analyze_shipment(ledger, ["seller-7"], "unsupported_claim")
    assert shipment_result["verdict"] == "on_time"
    assert shipment_result["timeline_complete"] is True
    assert analyze_payment(ledger, "unsupported_claim")["captured_total_brl"] == 89.0


def test_canceled_transaction_does_not_inherit_a_late_seller() -> None:
    late_row = {
        "order_status": "delivered",
        "order_purchase_timestamp": "2018-07-04T09:00:00-03:00",
        "order_delivered_carrier_date": "2018-07-11T09:00:00-03:00",
        "order_delivered_customer_date": "2018-07-18T09:00:00-03:00",
        "order_estimated_delivery_date": "2018-07-14T09:00:00-03:00",
    }
    canceled_row = {
        "order_status": "canceled",
        "order_purchase_timestamp": "2018-07-27T09:00:00-03:00",
        "order_delivered_carrier_date": "2018-07-29T09:00:00-03:00",
        "order_delivered_customer_date": None,
        "order_estimated_delivery_date": "2018-08-06T09:00:00-03:00",
    }
    items = EvidenceRecord(
        "CASE_008",
        "get_order_items",
        "item",
        "ev_items",
        [
            {"shipping_limit_date": "2018-07-07T09:00:00-03:00"},
            {"shipping_limit_date": "2018-07-30T09:00:00-03:00"},
        ],
    )
    ledger = _history_ledger("CASE_008", [late_row, canceled_row], [items])

    result = analyze_shipment(ledger, ["seller-8"], "canceled_order_paid")

    assert result == {
        "verdict": "insufficient_evidence",
        "late_seller_ids": [],
        "timeline_complete": False,
    }


def test_transport_failure_is_not_recorded_as_missing_evidence() -> None:
    class BrokenGateway:
        async def call(self, name: str, *, case_id: str, **arguments: Any) -> dict[str, Any]:
            raise GatewayUnavailable("MCP tool get_order timed out")

    ledger = EvidenceLedger("CASE_009")
    with pytest.raises(GatewayUnavailable):
        asyncio.run(ledger.call(BrokenGateway(), LIVE_TOOLS[1], {"order_id": "ORDER_009"}))
    assert ledger.errors == []
