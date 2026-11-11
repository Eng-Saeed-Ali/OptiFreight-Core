from __future__ import annotations
import json
import pytest

from optifreight.intake_gate import (
    MAX_PAYLOAD_BYTES,
    MAX_JSON_DEPTH,
    IntakeError,
    OfferRejection,
    parse_decimal_to_scaled_int,
    ingest_json_bundle_report,
    ingest_json_bundle,
)
from optifreight.cli_visualizer import build_demo_scenario, render_solution_report
from optifreight.optimizer import solve


def _minimal_valid_bundle_dict() -> dict[str, object]:
    return {
        "schema": "optifreight.bundle/v1",
        "start_node": "A",
        "truck": {
            "id": "T1",
            "trailer": "STANDARD_TILT",
            "base_cpkm": 100,
            "rate_cptkm": 2,
            "min_net_profit_eur": "0.00",
            "min_cents_per_km": 0,
        },
        "cities": [
            {"id": "A", "name": "City A", "backhaul_ev_eur": "0.00", "return_prob_permille": 0},
            {"id": "B", "name": "City B", "backhaul_ev_eur": "50.00", "return_prob_permille": 500},
        ],
        "edges": [
            {"origin_id": "A", "dest_id": "B", "distance_km": "100.000", "drive_min": 60, "toll_eur": "10.00"},
        ],
        "offers": [
            {
                "id": "O1",
                "origin_id": "A",
                "dest_id": "B",
                "weight_t": "5.000",
                "pallet_count": 6,
                "pallet_len_m": "1.200",
                "pallets_per_row": 3,
                "pallet_height_m": "1.200",
                "is_stackable": False,
                "required_trailer": "STANDARD_TILT",
                "cargo_class": "GENERAL",
                "earliest_pickup_min": 0,
                "latest_pickup_min": 100,
                "earliest_delivery_min": 100,
                "latest_delivery_min": 300,
                "revenue_eur": "500.00",
                "dock_dwell_min": 30,
            }
        ],
    }


def test_payload_byte_cap() -> None:
    bundle = _minimal_valid_bundle_dict()
    base_json = json.dumps(bundle)
    base_len = len(base_json.encode("utf-8"))

    # Pad with spaces to exactly MAX_PAYLOAD_BYTES (4 MiB)
    pad_needed = MAX_PAYLOAD_BYTES - base_len
    valid_exact = base_json + (" " * pad_needed)
    assert len(valid_exact.encode("utf-8")) == MAX_PAYLOAD_BYTES
    g, tr, off, sn = ingest_json_bundle(valid_exact)
    assert sn == "A"
    assert len(off) == 1

    # 4 MiB + 1 byte -> rejected
    too_large = valid_exact + " "
    assert len(too_large.encode("utf-8")) == MAX_PAYLOAD_BYTES + 1
    with pytest.raises(IntakeError) as exc_info:
        ingest_json_bundle(too_large)
    assert exc_info.value.code == "PAYLOAD_TOO_LARGE"

    # Multi-byte UTF-8 character '€' takes 3 bytes
    # 1_500_000 '€' characters = 4_500_000 bytes > 4 MiB
    utf8_large = "€" * 1_500_000
    with pytest.raises(IntakeError) as exc_info2:
        ingest_json_bundle(utf8_large)
    assert exc_info2.value.code == "PAYLOAD_TOO_LARGE"


def test_depth_pre_scan() -> None:
    # Depth 8 accepted
    d8_str = '{"a": {"b": {"c": {"d": {"e": {"f": {"g": 1}}}}}}}'
    # Depth 9 rejected
    d9_str = '{"a": {"b": {"c": {"d": {"e": {"f": {"g": {"h": {"i": 1}}}}}}}}}'
    with pytest.raises(IntakeError) as exc:
        ingest_json_bundle(d9_str)
    assert exc.value.code == "DEPTH_EXCEEDED"

    # Brackets inside strings must NOT count toward depth
    bundle = _minimal_valid_bundle_dict()
    bundle["cities"][0]["name"] = "[[[[[[[[[[City A with brackets]]]]]]]]]]"
    raw = json.dumps(bundle)
    g, tr, off, sn = ingest_json_bundle(raw)
    assert g.nodes["A"].name == "[[[[[[[[[[City A with brackets]]]]]]]]]]"


def test_parse_decimal_to_scaled_int() -> None:
    assert parse_decimal_to_scaled_int("1450.50", 100) == 145_050
    assert parse_decimal_to_scaled_int("24.0", 1000) == 24_000
    assert parse_decimal_to_scaled_int("1.20", 1000) == 1_200
    assert parse_decimal_to_scaled_int("350", 1000) == 350_000
    assert parse_decimal_to_scaled_int("0.00", 100) == 0
    assert parse_decimal_to_scaled_int("0", 1) == 0

    # Rejections
    for bad in ("1e999999", "NaN", "Infinity", "-1.00", "1.2345", "٣", "01", "1\x00", "", " "):
        with pytest.raises(ValueError):
            parse_decimal_to_scaled_int(bad, 1000)

    # Scale 1 disallows fractional digits
    with pytest.raises(ValueError):
        parse_decimal_to_scaled_int("1.0", 1)

    # Invalid scale
    with pytest.raises(ValueError):
        parse_decimal_to_scaled_int("10", 5)


def test_duplicate_keys_and_non_finite() -> None:
    # Duplicate key rejected
    dup_json = '{"schema":"optifreight.bundle/v1","schema":"v2"}'
    with pytest.raises(IntakeError) as exc1:
        ingest_json_bundle(dup_json)
    assert exc1.value.code == "DUPLICATE_KEY"

    # Non-finite NaN rejected
    nan_json = '{"schema":"optifreight.bundle/v1","val": NaN}'
    with pytest.raises(IntakeError) as exc2:
        ingest_json_bundle(nan_json)
    assert exc2.value.code == "NON_FINITE_NUMBER"


def test_bare_json_numbers_without_float() -> None:
    bundle = _minimal_valid_bundle_dict()
    # Replace string decimals with unquoted JSON numbers: 5.000, 1.2, 500.50
    raw = json.dumps(bundle).replace('"5.000"', "5.000").replace('"1.200"', "1.2").replace('"500.00"', "500.50")
    g, tr, off, sn = ingest_json_bundle(raw)
    assert off[0].weight_kg == 5_000
    assert off[0].pallet_len_mm == 1_200
    assert off[0].revenue_cents == 50_050


def test_partial_offer_rejection() -> None:
    bundle = _minimal_valid_bundle_dict()
    # Add 2 good offers and 2 malformed offers
    good1 = dict(bundle["offers"][0])
    good1["id"] = "O2"
    good2 = dict(bundle["offers"][0])
    good2["id"] = "O3"

    bad_node = dict(bundle["offers"][0])
    bad_node["id"] = "BAD1"
    bad_node["dest_id"] = "UNKNOWN_CITY"

    bad_field = dict(bundle["offers"][0])
    bad_field["id"] = "BAD2"
    bad_field["weight_t"] = "999.0"  # Exceeds MAX_WEIGHT_KG (24t)

    bundle["offers"] = [bundle["offers"][0], bad_node, good1, bad_field, good2]
    raw = json.dumps(bundle)

    g, tr, valid_offers, sn, rejections = ingest_json_bundle_report(raw)
    assert len(valid_offers) == 3
    assert tuple(o.id for o in valid_offers) == ("O1", "O2", "O3")
    assert len(rejections) == 2
    assert rejections[0].index == 1
    assert rejections[0].offer_id == "BAD1"
    assert rejections[0].code == "UNKNOWN_NODE"
    assert rejections[1].index == 3
    assert rejections[1].offer_id == "BAD2"
    assert rejections[1].code == "MALFORMED_OFFER"


def test_round_trip_equivalence() -> None:
    graph, truck, offers, start_node = build_demo_scenario()
    bundle = {
        "schema": "optifreight.bundle/v1",
        "start_node": start_node,
        "truck": {
            "id": truck.id,
            "trailer": truck.trailer.value,
            "base_cpkm": truck.base_cpkm,
            "rate_cptkm": truck.rate_cptkm,
            "min_net_profit_eur": "0.00",
            "min_cents_per_km": truck.min_cents_per_km,
        },
        "cities": [
            {
                "id": c.id,
                "name": c.name,
                "backhaul_ev_eur": f"{c.backhaul_ev_cents // 100}.{c.backhaul_ev_cents % 100:02d}",
                "return_prob_permille": c.return_prob_permille,
            }
            for c in graph.nodes.values()
        ],
        "edges": [
            {
                "origin_id": e.origin_id,
                "dest_id": e.dest_id,
                "distance_km": f"{e.distance_m // 1000}.{e.distance_m % 1000:03d}",
                "drive_min": e.drive_min,
                "toll_eur": f"{e.toll_cents // 100}.{e.toll_cents % 100:02d}",
            }
            for el in graph.adj.values()
            for e in el
        ],
        "offers": [
            {
                "id": o.id,
                "origin_id": o.origin_id,
                "dest_id": o.dest_id,
                "weight_t": f"{o.weight_kg // 1000}.{o.weight_kg % 1000:03d}",
                "pallet_count": o.pallet_count,
                "pallet_len_m": f"{o.pallet_len_mm // 1000}.{o.pallet_len_mm % 1000:03d}",
                "pallets_per_row": o.pallets_per_row,
                "pallet_height_m": f"{o.pallet_height_mm // 1000}.{o.pallet_height_mm % 1000:03d}",
                "is_stackable": o.is_stackable,
                "required_trailer": o.required_trailer.value,
                "cargo_class": o.cargo_class.value,
                "earliest_pickup_min": o.earliest_pickup_min,
                "latest_pickup_min": o.latest_pickup_min,
                "earliest_delivery_min": o.earliest_delivery_min,
                "latest_delivery_min": o.latest_delivery_min,
                "revenue_eur": f"{o.revenue_cents // 100}.{o.revenue_cents % 100:02d}",
                "dock_dwell_min": o.dock_dwell_min,
            }
            for o in offers
        ],
    }
    raw = json.dumps(bundle)
    g2, tr2, off2, sn2 = ingest_json_bundle(raw)
    sol2 = solve(g2, tr2, off2, sn2)
    sol_orig = solve(graph, truck, offers, start_node)
    assert sol2 is not None and sol_orig is not None
    rep2 = render_solution_report(sol2, {o.id: o for o in off2}, tr2.trailer)
    rep_orig = render_solution_report(sol_orig, {o.id: o for o in offers}, truck.trailer)
    assert rep2 == rep_orig


def test_step7_envelope_hardening_and_closed_world() -> None:
    """Step 7 / Phase 2-B hardening: bool smuggling, closed-world cities/edges, lone surrogates, non-str ids."""
    def _code(mutate) -> str:
        d = _minimal_valid_bundle_dict()
        mutate(d)
        with pytest.raises(IntakeError) as ei:
            ingest_json_bundle(json.dumps(d))
        return ei.value.code

    assert _code(lambda d: d["edges"][0].__setitem__("distance_km", True)) == "INVALID_ENVELOPE"  # bool is not an int
    assert _code(lambda d: d["cities"][0].__setitem__("evil", 1)) == "INVALID_ENVELOPE"
    assert _code(lambda d: d["edges"][0].__setitem__("speed", 1)) == "INVALID_ENVELOPE"
    assert _code(lambda d: d["cities"][1].pop("name")) == "INVALID_ENVELOPE"
    assert _code(lambda d: d["cities"].__setitem__(0, "A")) == "INVALID_ENVELOPE"
    assert _code(lambda d: d["truck"].__setitem__("min_net_profit_eur", "1e3")) == "INVALID_ENVELOPE"
    with pytest.raises(IntakeError) as ei:
        ingest_json_bundle("\ud800")
    assert ei.value.code == "INVALID_UTF8"

    d = _minimal_valid_bundle_dict()
    bad = dict(d["offers"][0], id=["O9"])
    d["offers"] = [d["offers"][0], bad]
    _, _, offers, _, rej = ingest_json_bundle_report(json.dumps(d))
    assert [o.id for o in offers] == ["O1"]
    assert rej == (OfferRejection(1, None, "MALFORMED_OFFER", rej[0].detail),)
