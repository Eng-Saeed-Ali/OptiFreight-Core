from __future__ import annotations
import ast, random
from pathlib import Path
from optifreight.optimizer import solve
from tests._oracle import brute_force_solve, generate_random_instance, verify_solution

ALLOWLIST = {
    "models.py": {"__future__", "re", "dataclasses", "enum"},
    "graph.py": {"__future__", "collections.abc", "dataclasses", "heapq", "itertools", "optifreight.models"},
    "physics.py": {"__future__", "collections.abc", "optifreight.models", "optifreight.graph"},
    "optimizer.py": {"__future__", "collections.abc", "dataclasses", "heapq", "itertools", "optifreight.models", "optifreight.graph", "optifreight.physics"},
    "intake_gate.py": {"__future__", "collections.abc", "dataclasses", "enum", "json", "re", "optifreight.models", "optifreight.graph"},
    "cli_visualizer.py": {"__future__", "argparse", "collections.abc", "pathlib", "optifreight.models", "optifreight.graph", "optifreight.intake_gate", "optifreight.optimizer"},
    "__init__.py": {"__future__", "optifreight.models", "optifreight.graph", "optifreight.physics", "optifreight.optimizer", "optifreight.cli_visualizer", "optifreight.intake_gate"},
}

def test_ff1_zero_float_and_division_ast() -> None:
    root = Path(__file__).resolve().parent.parent
    py_files = list(root.joinpath("src").rglob("*.py")) + list(root.joinpath("tests").rglob("*.py"))
    assert len(py_files) >= 10
    violations, banned = [], {"math", "decimal", "fractions", "statistics"}
    for p in py_files:
        tree = ast.parse(p.read_text(encoding="utf-8"), filename=str(p))
        is_src = "src" in p.parts
        for n in ast.walk(tree):
            if isinstance(n, ast.Constant) and isinstance(n.value, (float, complex)):
                violations.append(f"{p.name}:{n.lineno} float or complex constant")
            if isinstance(n, ast.Div):
                violations.append(f"{p.name}:{n.lineno} Div operator")
            if isinstance(n, ast.Call):
                if isinstance(n.func, ast.Name) and n.func.id in ("float", "complex"):
                    violations.append(f"{p.name}:{n.lineno} Call to {n.func.id}")
                elif isinstance(n.func, ast.Attribute) and n.func.attr == "fromhex":
                    violations.append(f"{p.name}:{n.lineno} Call to .fromhex")
            if is_src and isinstance(n, (ast.Import, ast.ImportFrom)):
                mod = n.module if isinstance(n, ast.ImportFrom) else ""
                names = [a.name for a in n.names]
                if mod in banned or any(x in banned for x in names):
                    violations.append(f"{p.name}:{n.lineno} banned math import")
    assert not violations, "FF-1 violations:\n" + "\n".join(violations)

def test_ff2_functional_core_import_isolation_ast() -> None:
    src_dir = Path(__file__).resolve().parent.parent.joinpath("src", "optifreight")
    violations, banned_calls = [], {"eval", "exec", "compile", "open", "__import__", "input"}
    for p in src_dir.glob("*.py"):
        allowed = ALLOWLIST.get(p.name)
        if allowed is None: violations.append(f"{p.name}: module has no import allowlist entry"); continue
        tree = ast.parse(p.read_text(encoding="utf-8"), filename=str(p))
        for n in ast.walk(tree):
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id in banned_calls:
                violations.append(f"{p.name}:{n.lineno} banned call {n.func.id}")
            if allowed is not None:
                if isinstance(n, ast.Import):
                    for a in n.names:
                        if a.name not in allowed: violations.append(f"{p.name}:{n.lineno} import {a.name}")
                elif isinstance(n, ast.ImportFrom) and n.module not in allowed:
                    violations.append(f"{p.name}:{n.lineno} from {n.module}")
    assert not violations, "FF-2 violations:\n" + "\n".join(violations)

def test_ff3_immutable_slot_dataclasses_ast() -> None:
    src_dir = Path(__file__).resolve().parent.parent.joinpath("src", "optifreight")
    found, violations = set(), []
    for p in src_dir.glob("*.py"):
        tree = ast.parse(p.read_text(encoding="utf-8"), filename=str(p))
        for n in ast.walk(tree):
            if isinstance(n, ast.ClassDef):
                is_dc = lambda f: (isinstance(f, ast.Name) and f.id == "dataclass") or (isinstance(f, ast.Attribute) and f.attr == "dataclass")
                dcs = [d for d in n.decorator_list if is_dc(d) or (isinstance(d, ast.Call) and is_dc(d.func))]
                if not dcs: continue
                found.add(n.name)
                for d in dcs:
                    if not isinstance(d, ast.Call): violations.append(f"{p.name}:{n.name} bare @dataclass"); continue
                    kw = {k.arg: (k.value.value if isinstance(k.value, ast.Constant) else None) for k in d.keywords}
                    if kw.get("slots") is not True or kw.get("frozen") is not True:
                        violations.append(f"{p.name}:{n.name} missing slots=True or frozen=True")
    expected = {"CityNode", "RoadEdge", "ShipmentOffer", "TruckSpec", "TachoState", "RouteStep", "TruckState", "Leg", "Solution", "OfferRejection"}
    assert found == expected, f"Dataclasses mismatch: {found} != {expected}"
    assert not violations, "FF-3 violations:\n" + "\n".join(violations)

def _fuzz(seed: int, n: int) -> dict[str, int]:
    rng, cov = random.Random(seed), {"feasible": 0, "multi": 0, "breaks": 0, "rests": 0}
    for seed_idx in range(n):
        graph, truck, offers, start_node = generate_random_instance(rng)
        sol = solve(graph, truck, offers, start_node)
        oracle = brute_force_solve(graph, truck, offers, start_node)
        assert (sol is None) == (oracle is None), f"Seed {seed}:{seed_idx}: sol={sol is None} oracle={oracle is None}"
        if sol is not None:
            verify_solution(sol, graph, truck, offers, start_node)
            assert sol.proven_optimal is True, f"Seed {seed}:{seed_idx} not proven optimal"
            ev_seq = tuple((st.kind.value, st.offer_id) for st in sol.steps)
            assert (sol.net_profit_cents, ev_seq) == oracle, f"Seed {seed}:{seed_idx}: sol={(sol.net_profit_cents, ev_seq)} oracle={oracle}"
            assert solve(graph, truck, offers, start_node).steps == sol.steps, f"Seed {seed}:{seed_idx} non-deterministic"
            cov["feasible"] += 1; cov["multi"] += len(sol.delivered_ids) > 1
            cov["breaks"] += any(s.breaks_on_leg for s in sol.steps); cov["rests"] += any(s.rests_on_leg for s in sol.steps)
    return cov

def test_ff4_seeded_oracle_fuzzer_50_instances() -> None:
    cov = _fuzz(42, 50)
    assert cov["feasible"] >= 15 and cov["rests"] >= 3, cov  # non-vacuous: tacho rests really occur

def test_ff4b_deep_oracle_fuzzer_1000_instances_with_coverage_floors() -> None:
    cov = _fuzz(2026, 1000)  # coverage floors stop a future generator edit from silently going vacuous
    assert cov["feasible"] >= 300 and cov["multi"] >= 50 and cov["breaks"] >= 300 and cov["rests"] >= 100, cov

