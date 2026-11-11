from __future__ import annotations
import re
from dataclasses import dataclass, field
from enum import Enum

MAX_WEIGHT_KG: int = 24_000; MAX_LDM_MM: int = 13_600
CLEARANCE_STANDARD_MM: int = 2_700; CLEARANCE_MEGA_MM: int = 3_000
KINGPIN_X_MM: int = 1_600; TRAILER_AXLE_X_MM: int = 10_300
FIFTH_WHEEL_DRIVE_SHARE_PERMILLE: int = 850
TRACTOR_DRIVE_TARE_KG: int = 800; TRAILER_TARE_KINGPIN_KG: int = 2_000; MAX_DRIVE_AXLE_KG: int = 11_500
TACHO_CONT_MIN: int = 270; TACHO_BREAK_MIN: int = 45; TACHO_DAY_MIN: int = 540
TACHO_REST_MIN: int = 660; TACHO_AMP_MIN: int = 900

class TrailerType(Enum):
    STANDARD_TILT, MEGA, REEFER = "STANDARD_TILT", "MEGA", "REEFER"

class CargoClass(Enum):
    GENERAL, ADR_HAZMAT, FOOD_PHARMA, FRAGILE_ELECTRONICS = "GENERAL", "ADR_HAZMAT", "FOOD_PHARMA", "FRAGILE_ELECTRONICS"

class EventKind(Enum):
    PICKUP, DELIVER = "PICKUP", "DELIVER"

def ceil_div(n: int, d: int) -> int: return -(-n // d)
def clearance_mm(trailer: TrailerType) -> int: return CLEARANCE_MEGA_MM if trailer is TrailerType.MEGA else CLEARANCE_STANDARD_MM

def ldm_for(offer: ShipmentOffer, trailer: TrailerType) -> int | None:
    c = clearance_mm(trailer)
    if offer.pallet_height_mm > c: return None
    tiers = (c // offer.pallet_height_mm) if offer.is_stackable else 1
    return ceil_div(offer.pallet_count, offer.pallets_per_row * tiers) * offer.pallet_len_mm

_ID_RE = re.compile(r"^[A-Z0-9_-]{1,32}\Z")
def _chk_id(s: str) -> None:
    if not isinstance(s, str) or not _ID_RE.match(s) or "\x00" in s: raise ValueError(f"Invalid ID: {s!r}")

def _chk_i(v: int, low: int, high: int | None = None) -> None:
    if type(v) is not int or v < low or (high is not None and v > high): raise ValueError(f"Invalid int: {v!r}")

@dataclass(slots=True, frozen=True)
class CityNode:
    id: str; name: str; backhaul_ev_cents: int = 0; return_prob_permille: int = 0
    def __post_init__(self) -> None:
        _chk_id(self.id); _chk_i(self.backhaul_ev_cents, 0); _chk_i(self.return_prob_permille, 0, 1000)
        if not isinstance(self.name, str) or not self.name or "\x00" in self.name: raise ValueError("Invalid name")

@dataclass(slots=True, frozen=True)
class RoadEdge:
    origin_id: str; dest_id: str; distance_m: int; drive_min: int; toll_cents: int = 0
    def __post_init__(self) -> None:
        _chk_id(self.origin_id); _chk_id(self.dest_id)
        if self.origin_id == self.dest_id: raise ValueError("origin == dest")
        _chk_i(self.distance_m, 1); _chk_i(self.drive_min, 1); _chk_i(self.toll_cents, 0)

@dataclass(slots=True, frozen=True)
class ShipmentOffer:
    id: str; origin_id: str; dest_id: str; weight_kg: int; pallet_count: int; pallet_len_mm: int
    pallets_per_row: int; pallet_height_mm: int; is_stackable: bool; required_trailer: TrailerType
    cargo_class: CargoClass; earliest_pickup_min: int; latest_pickup_min: int
    earliest_delivery_min: int; latest_delivery_min: int; revenue_cents: int; dock_dwell_min: int = 45
    def __post_init__(self) -> None:
        _chk_id(self.id); _chk_id(self.origin_id); _chk_id(self.dest_id)
        if self.origin_id == self.dest_id: raise ValueError("origin == dest")
        _chk_i(self.weight_kg, 1, MAX_WEIGHT_KG); _chk_i(self.pallet_count, 1)
        _chk_i(self.pallet_len_mm, 1); _chk_i(self.pallets_per_row, 1); _chk_i(self.pallet_height_mm, 1, CLEARANCE_MEGA_MM)
        if type(self.is_stackable) is not bool: raise ValueError("is_stackable must be bool")
        if not isinstance(self.required_trailer, TrailerType) or not isinstance(self.cargo_class, CargoClass): raise ValueError("Invalid enum")
        _chk_i(self.earliest_pickup_min, 0); _chk_i(self.latest_pickup_min, self.earliest_pickup_min)
        _chk_i(self.earliest_delivery_min, self.earliest_pickup_min); _chk_i(self.latest_delivery_min, self.earliest_delivery_min)
        _chk_i(self.revenue_cents, 1); _chk_i(self.dock_dwell_min, 1)
        r_ldm, m_ldm = ldm_for(self, self.required_trailer), ldm_for(self, TrailerType.MEGA)
        if r_ldm is None or r_ldm > MAX_LDM_MM or m_ldm is None or m_ldm > MAX_LDM_MM: raise ValueError("Static LDM overflow")

@dataclass(slots=True, frozen=True)
class TruckSpec:
    id: str; trailer: TrailerType; base_cpkm: int; rate_cptkm: int; min_net_profit_cents: int = 0; min_cents_per_km: int = 0
    def __post_init__(self) -> None:
        _chk_id(self.id); _chk_i(self.base_cpkm, 0); _chk_i(self.rate_cptkm, 0); _chk_i(self.min_cents_per_km, 0)
        if not isinstance(self.trailer, TrailerType) or type(self.min_net_profit_cents) is not int: raise ValueError("Invalid truck spec")

@dataclass(slots=True, frozen=True)
class TachoState:
    clock: int; cont_drive: int; day_drive: int; shift_start: int; breaks_taken: int = 0; rests_taken: int = 0

@dataclass(slots=True, frozen=True)
class RouteStep:
    kind: EventKind; offer_id: str; city_id: str; arrival_min: int; start_min: int; end_min: int; leg_dist_m: int
    leg_cost_cents: int; breaks_on_leg: int; rests_on_leg: int; used_kg_after: int; used_ldm_after: int
    drive_axle_kg_after: int; onboard_after: tuple[str, ...]
    prev: RouteStep | None = field(default=None, compare=False, repr=False)

@dataclass(slots=True, frozen=True)
class TruckState:
    node: str; clock: int; onboard: tuple[str, ...]; delivered: frozenset[str]; picked: frozenset[str]
    used_kg: int; used_ldm: int; cont_drive: int; day_drive: int; shift_start: int; adr_tainted: bool
    revenue: int; cost: int; dist_m: int
    last_step: RouteStep | None = field(default=None, compare=False, repr=False)
