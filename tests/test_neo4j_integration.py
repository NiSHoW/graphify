"""Optional integration tests against a REAL Neo4j instance.

Skipped unless NEO4J_TEST_URI is set. Run locally with e.g.:

    docker run --rm -p 7687:7687 -e NEO4J_AUTH=neo4j/testpassword neo4j:5
    NEO4J_TEST_URI=neo4j://localhost:7687 NEO4J_TEST_PASSWORD=testpassword \
        pytest tests/test_neo4j_integration.py

Uses throwaway branch names so it can run against a shared dev instance.
"""
import json
import os
import uuid

import pytest

pytestmark = pytest.mark.skipif(
    not os.environ.get("NEO4J_TEST_URI"),
    reason="NEO4J_TEST_URI not set (integration test needs a live Neo4j)",
)


@pytest.fixture
def backend():
    from graphify.backends.neo4j import Neo4jBackend
    b = Neo4jBackend(
        os.environ["NEO4J_TEST_URI"],
        os.environ.get("NEO4J_TEST_USER", "neo4j"),
        os.environ.get("NEO4J_TEST_PASSWORD", os.environ.get("NEO4J_PASSWORD", "")),
        database=os.environ.get("NEO4J_TEST_DATABASE", "neo4j"),
        branch=f"it-{uuid.uuid4().hex[:8]}",
    )
    b.ensure_schema()
    yield b
    b.delete_branch(b.branch)
    b.close()


def _data():
    return {
        "directed": False, "multigraph": False,
        "graph": {"custom": "attr"},
        "nodes": [
            {"id": "n1", "label": "alpha", "file_type": "code", "kind": "function",
             "source_file": "a.py", "community": 0, "_origin": "ast"},
            {"id": "n2", "label": "beta", "file_type": "document", "community": None},
        ],
        "links": [
            {"source": "n1", "target": "n2", "relation": "calls-into",
             "confidence": "EXTRACTED", "context": "call", "weight": 2},
        ],
        "hyperedges": [{"id": "he1", "nodes": ["n1", "n2"], "label": "shared"}],
        "built_at_commit": "deadbee",
    }


def test_full_save_load_round_trip(backend):
    data = _data()
    counts = backend.save_graph_data(data, prior=None)
    assert counts["nodes"] == 2 and counts["edges"] == 1
    assert backend.get_version() == 1

    loaded = backend.load_graph_data()
    assert {n["id"]: n for n in loaded["nodes"]} == {n["id"]: n for n in data["nodes"]}
    assert loaded["links"] == data["links"]
    assert loaded["hyperedges"] == data["hyperedges"]
    assert loaded["graph"] == data["graph"]
    assert loaded["built_at_commit"] == "deadbee"


def test_delta_and_version_bump(backend):
    prior = _data()
    backend.save_graph_data(prior, prior=None)
    new = _data()
    new["nodes"] = [n for n in new["nodes"] if n["id"] != "n2"]
    new["nodes"].append({"id": "n3", "label": "gamma", "file_type": "code"})
    new["links"] = []
    counts = backend.save_graph_data(new, prior=prior)
    assert counts["nodes_removed"] == 1
    assert backend.get_version() == 2

    loaded = backend.load_graph_data()
    assert {n["id"] for n in loaded["nodes"]} == {"n1", "n3"}
    assert loaded["links"] == []

    # empty delta must not bump the version
    backend.save_graph_data(new, prior=new)
    assert backend.get_version() == 2


def test_branch_isolation_and_listing(backend):
    from graphify.backends.neo4j import Neo4jBackend
    backend.save_graph_data(_data(), prior=None)
    other = Neo4jBackend(
        os.environ["NEO4J_TEST_URI"],
        os.environ.get("NEO4J_TEST_USER", "neo4j"),
        os.environ.get("NEO4J_TEST_PASSWORD", os.environ.get("NEO4J_PASSWORD", "")),
        database=os.environ.get("NEO4J_TEST_DATABASE", "neo4j"),
        branch=backend.branch + "-other",
    )
    try:
        other.save_graph_data({"nodes": [{"id": "only", "label": "x", "file_type": "code"}],
                               "links": [], "graph": {}, "hyperedges": []}, prior=None)
        assert {n["id"] for n in backend.load_graph_data()["nodes"]} == {"n1", "n2"}
        assert {n["id"] for n in other.load_graph_data()["nodes"]} == {"only"}
        branches = {m["branch"] for m in backend.list_branches()}
        assert {backend.branch, other.branch} <= branches
    finally:
        other.delete_branch(other.branch)
        other.close()
    assert other.branch not in {m["branch"] for m in backend.list_branches()}
