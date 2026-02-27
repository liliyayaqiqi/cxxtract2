from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[1]


def _find_extractor_binary() -> Path | None:
    candidates = [
        REPO_ROOT / "cpp-extractor" / "build" / "Release" / "cpp-extractor.exe",
        REPO_ROOT / "cpp-extractor" / "build" / "RelWithDebInfo" / "cpp-extractor.exe",
        REPO_ROOT / "cpp-extractor" / "build" / "Debug" / "cpp-extractor.exe",
        REPO_ROOT / "cpp-extractor" / "build" / "cpp-extractor.exe",
        REPO_ROOT / "bin" / "cpp-extractor.exe",
    ]
    for candidate in candidates:
        if candidate.exists():
            return candidate
    return None


def _run_extractor(source: Path, *clang_args: str) -> dict:
    extractor = _find_extractor_binary()
    if extractor is None:
        pytest.skip("cpp-extractor.exe not found; build cpp-extractor to run precision tests")

    cmd = [
        str(extractor),
        "--action",
        "extract-all",
        "--file",
        str(source),
        "--",
        *clang_args,
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if proc.returncode != 0:
        raise AssertionError(f"extractor failed with exit code {proc.returncode}: {proc.stderr}")

    raw = proc.stdout.strip() or proc.stderr.strip()
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise AssertionError(f"extractor did not return JSON: {raw}") from exc

    assert payload["success"] is True, payload["diagnostics"]
    return payload


def test_member_calls_emit_call_edges_and_precise_ref_kinds(tmp_path: Path):
    src = tmp_path / "precision.cpp"
    src.write_text(
        """
        namespace demo {

        struct IConnection {
            virtual ~IConnection() = default;
            virtual bool applyConfigs(int value) = 0;
        };

        inline void helper() {}

        struct Connection : IConnection {
            int field = 0;

            bool applyConfigs(int value) override {
                field = value;
                ++field;
                return field > 0;
            }

            void touch() {
                field = 1;
                ++field;
                auto field_ptr = &field;
                (void)field_ptr;
                auto helper_ptr = &helper;
                (void)helper_ptr;
                (helper)();
                this->applyConfigs(field);
            }
        };

        struct Wrapper {
            IConnection* conn = nullptr;
        };

        void drive(IConnection* conn) {
            Connection local;
            Wrapper wrapper{conn};
            conn->applyConfigs(1);
            wrapper.conn->applyConfigs(2);
            local.applyConfigs(3);
            local.touch();
        }

        }  // namespace demo
        """,
        encoding="utf-8",
    )

    payload = _run_extractor(src, "-std=c++17")

    references = payload["references"]
    call_refs = [ref for ref in references if ref["kind"] == "call"]
    write_refs = [ref for ref in references if ref["kind"] == "write"]
    addr_refs = [ref for ref in references if ref["kind"] == "addr"]
    call_edges = payload["call_edges"]

    apply_call_symbols = {ref["symbol"] for ref in call_refs if ref["symbol"].endswith("applyConfigs")}
    assert "demo::Connection::applyConfigs" in apply_call_symbols
    assert any(symbol in {"demo::IConnection::applyConfigs", "demo::Connection::applyConfigs"} for symbol in apply_call_symbols)

    helper_call_refs = [ref for ref in call_refs if ref["symbol"] == "demo::helper"]
    assert helper_call_refs, "expected wrapped free-function call to emit a call reference"

    apply_edges = [edge for edge in call_edges if edge["callee"].endswith("applyConfigs")]
    assert apply_edges, "expected at least one applyConfigs call edge"
    assert any(edge["caller"] == "demo::drive" for edge in apply_edges)

    write_symbols = {ref["symbol"] for ref in write_refs}
    assert "demo::Connection::field" in write_symbols

    addr_symbols = {ref["symbol"] for ref in addr_refs}
    assert "demo::Connection::field" in addr_symbols
    assert "demo::helper" in addr_symbols

    call_sites = {(ref["symbol"], ref["line"], ref["col"]) for ref in call_refs}
    read_sites = {
        (ref["symbol"], ref["line"], ref["col"])
        for ref in references
        if ref["kind"] == "read" and ref["symbol"].endswith("applyConfigs")
    }
    assert call_sites.isdisjoint(read_sites), "call references should shadow same-site read references"
