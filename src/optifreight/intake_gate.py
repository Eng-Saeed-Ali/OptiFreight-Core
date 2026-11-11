from __future__ import annotations
from dataclasses import dataclass
import json, re
from optifreight.models import CargoClass, CityNode, RoadEdge, ShipmentOffer, TrailerType, TruckSpec
from optifreight.graph import RoadGraph

MAX_PAYLOAD_BYTES: int = 4_194_304; MAX_JSON_DEPTH: int = 8
MAX_CITIES: int = 256; MAX_EDGES: int = 4_096; MAX_OFFERS: int = 64
MAX_DECIMAL_CHARS: int = 24; MAX_INT_DIGITS: int = 15

class IntakeError(ValueError):
    def __init__(self, code: str, detail: str = "") -> None:
        self.code, self.detail = code, str(detail)[:64]
        super().__init__(f"{code}: {self.detail}" if self.detail else code)

@dataclass(slots=True, frozen=True)
class OfferRejection:
    index: int; offer_id: str | None; code: str; detail: str

_DEC_RE = re.compile(r"^(0|[1-9][0-9]{0,14})(?:\.([0-9]+))?\Z")

def parse_decimal_to_scaled_int(s: str, scale: int) -> int:
    if scale not in (1, 10, 100, 1000): raise ValueError("Invalid scale")
    if type(s) is not str or not (1 <= len(s) <= MAX_DECIMAL_CHARS) or "\x00" in s:
        raise ValueError("Invalid decimal string")
    m = _DEC_RE.match(s)
    if not m: raise ValueError(f"Invalid decimal format: {s!r}")
    k = len(str(scale)) - 1
    w, f = m.group(1), m.group(2)
    if f is not None and (k == 0 or len(f) > k): raise ValueError("Excess precision")
    return int(w) * scale + (int(f.ljust(k, "0")) if f else 0)

def _scan_depth(text: str) -> None:
    depth, in_str, esc = 0, False, False
    for ch in text:
        if esc: esc = False
        elif ch == "\\":
            if in_str: esc = True
        elif ch == '"': in_str = not in_str
        elif not in_str:
            if ch in "{[":
                depth += 1
                if depth > MAX_JSON_DEPTH: raise IntakeError("DEPTH_EXCEEDED", f"{depth} > {MAX_JSON_DEPTH}")
            elif ch in "}]": depth -= 1

def _no_dup(pairs: list[tuple[str, object]]) -> dict[str, object]:
    res: dict[str, object] = {}
    for k, v in pairs:
        if k in res: raise IntakeError("DUPLICATE_KEY", k)
        res[k] = v
    return res

def _parse_dec(v: object, sc: int) -> int:
    return parse_decimal_to_scaled_int(str(v) if type(v) is int else v, sc)

def _keys(o: object, req: set[str], opt: set[str]) -> dict:
    if type(o) is not dict or set(o) - req - opt or req - set(o): raise ValueError("Schema keys mismatch")
    return o

_CITY_REQ, _CITY_OPT = {"id", "name"}, {"backhaul_ev_eur", "return_prob_permille"}
_EDGE_REQ, _EDGE_OPT = {"origin_id", "dest_id", "distance_km", "drive_min"}, {"toll_eur"}

def ingest_json_bundle_report(raw: str | bytes) -> tuple[RoadGraph, TruckSpec, tuple[ShipmentOffer, ...], str, tuple[OfferRejection, ...]]:
    if not isinstance(raw, (str, bytes)): raise IntakeError("INVALID_TYPE", "Expected str or bytes")
    try: b = raw.encode("utf-8") if isinstance(raw, str) else raw
    except UnicodeEncodeError as e: raise IntakeError("INVALID_UTF8", str(e))  # lone surrogates in str input
    if len(b) > MAX_PAYLOAD_BYTES: raise IntakeError("PAYLOAD_TOO_LARGE", f"{len(b)} bytes")
    try: text = raw if isinstance(raw, str) else b.decode("utf-8")
    except UnicodeDecodeError as e: raise IntakeError("INVALID_UTF8", str(e))
    if "\x00" in text: raise IntakeError("NUL_BYTE", "Contains NUL")
    _scan_depth(text)
    def _b_int(s: str) -> int:
        if len(s.lstrip("-")) > MAX_INT_DIGITS: raise IntakeError("INT_OVERFLOW", s)
        return int(s)
    def _rej_const(s: str) -> None: raise IntakeError("NON_FINITE_NUMBER", s)
    try: data = json.loads(text, parse_float=str, parse_int=_b_int, parse_constant=_rej_const, object_pairs_hook=_no_dup)
    except json.JSONDecodeError as e: raise IntakeError("INVALID_JSON", str(e))
    if type(data) is not dict: raise IntakeError("INVALID_ROOT", "Root must be object")
    if set(data) != {"schema", "start_node", "truck", "cities", "edges", "offers"}: raise IntakeError("INVALID_SCHEMA_KEYS", "Keys mismatch")
    if data["schema"] != "optifreight.bundle/v1": raise IntakeError("UNSUPPORTED_SCHEMA", str(data["schema"]))
    sn = data["start_node"]
    if not isinstance(sn, str): raise IntakeError("INVALID_START_NODE", "Must be str")
    cr, er, ofr = data["cities"], data["edges"], data["offers"]
    if not isinstance(cr, list) or not (1 <= len(cr) <= MAX_CITIES): raise IntakeError("INVALID_CITIES", "Cities bounds")
    if not isinstance(er, list) or len(er) > MAX_EDGES: raise IntakeError("INVALID_EDGES", "Edges bounds")
    if not isinstance(ofr, list) or len(ofr) > MAX_OFFERS: raise IntakeError("INVALID_OFFERS", "Offers bounds")
    tr = data["truck"]
    if not isinstance(tr, dict) or set(tr) - {"id", "trailer", "base_cpkm", "rate_cptkm", "min_net_profit_eur", "min_cents_per_km"} or {"id", "trailer", "base_cpkm", "rate_cptkm"} - set(tr):
        raise IntakeError("INVALID_TRUCK", "Truck keys")
    try:
        p_eur = str(tr.get("min_net_profit_eur", "0.00"))
        p_cents = -_parse_dec(p_eur[1:], 100) if p_eur.startswith("-") else _parse_dec(p_eur, 100)
        truck = TruckSpec(tr["id"], TrailerType(tr["trailer"]), tr["base_cpkm"], tr["rate_cptkm"], p_cents, tr.get("min_cents_per_km", 0))
        cities = tuple(CityNode(c["id"], c["name"], _parse_dec(c.get("backhaul_ev_eur", "0.00"), 100), c.get("return_prob_permille", 0)) for c in (_keys(x, _CITY_REQ, _CITY_OPT) for x in cr))
        edges = tuple(RoadEdge(e["origin_id"], e["dest_id"], _parse_dec(e["distance_km"], 1000), e["drive_min"], _parse_dec(e.get("toll_eur", "0.00"), 100)) for e in (_keys(x, _EDGE_REQ, _EDGE_OPT) for x in er))
    except (KeyError, ValueError, TypeError) as e: raise IntakeError("INVALID_ENVELOPE", str(e))
    try: graph = RoadGraph(cities, edges)
    except ValueError as e: raise IntakeError("GRAPH_BUILD_ERROR", str(e))
    if sn not in graph.nodes: raise IntakeError("UNKNOWN_START_NODE", sn)
    offers, rej, seen = [], [], set()
    req_o = {"id", "origin_id", "dest_id", "weight_t", "pallet_count", "pallet_len_m", "pallets_per_row", "pallet_height_m", "is_stackable", "required_trailer", "cargo_class", "earliest_pickup_min", "latest_pickup_min", "earliest_delivery_min", "latest_delivery_min", "revenue_eur"}
    for idx, o in enumerate(ofr):
        oid = o.get("id") if isinstance(o, dict) and type(o.get("id")) is str else None
        try:
            if not isinstance(o, dict) or set(o) - (req_o | {"dock_dwell_min"}) or req_o - set(o): raise ValueError("Invalid offer schema")
            if oid in seen: raise ValueError(f"Duplicate offer id {oid}")
            if o["origin_id"] not in graph.nodes or o["dest_id"] not in graph.nodes: raise ValueError("Unknown node")
            sp = ShipmentOffer(oid, o["origin_id"], o["dest_id"], _parse_dec(o["weight_t"], 1000), o["pallet_count"], _parse_dec(o["pallet_len_m"], 1000), o["pallets_per_row"], _parse_dec(o["pallet_height_m"], 1000), o["is_stackable"], TrailerType(o["required_trailer"]), CargoClass(o["cargo_class"]), o["earliest_pickup_min"], o["latest_pickup_min"], o["earliest_delivery_min"], o["latest_delivery_min"], _parse_dec(o["revenue_eur"], 100), o.get("dock_dwell_min", 45))
            seen.add(oid); offers.append(sp)
        except Exception as e:
            code = "UNKNOWN_NODE" if "Unknown node" in str(e) else ("DUPLICATE_OFFER_ID" if "Duplicate offer" in str(e) else "MALFORMED_OFFER")
            rej.append(OfferRejection(idx, oid, code, str(e)[:64]))
    return graph, truck, tuple(sorted(offers, key=lambda x: x.id)), sn, tuple(rej)

def ingest_json_bundle(raw: str | bytes) -> tuple[RoadGraph, TruckSpec, tuple[ShipmentOffer, ...], str]:
    return ingest_json_bundle_report(raw)[:4]

