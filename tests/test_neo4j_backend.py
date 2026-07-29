"""Tests for graphify/backends — Neo4j backend serialization, delta, config.

Two layers, neither needing a running Neo4j:
  - pure round-trip tests over the row builders and their inverse;
  - fake-driver tests: a stub ``neo4j`` module injected into sys.modules records
    every (query, params) pair so we can assert Cypher shape, batching, single
    transaction per save, version-bump-last, and that the password never leaks
    into a query string.
"""
import json
import sys
import types

import pytest

from graphify.backends import (
    backend_config,
    current_branch,
    ensure_out_gitignore,
    is_backend_ref,
    parse_backend_uri,
)
from graphify.backends.neo4j import (
    _graph_data_from_records,
    _node_rows,
    _edge_rows,
    compute_delta,
    node_uid,
    safe_label,
    safe_rel,
)


def _sample_data() -> dict:
    """A graph.json-shaped dict exercising every round-trip hazard: underscore
    keys, None values, non-scalar values, non-ASCII, hyperedges, graph attrs."""
    return {
        "directed": False,
        "multigraph": False,
        "graph": {"hyperedges": [], "custom": "attr"},
        "nodes": [
            {"id": "n1", "label": "esträct", "norm_label": "estract",
             "source_file": "a.py", "source_location": "L10",
             "file_type": "code", "kind": "function", "community": 0,
             "community_name": "Core", "_origin": "ast"},
            {"id": "n2", "label": "helper", "source_file": "b.py",
             "file_type": "document", "community": None,
             "tags": ["x", "y"]},
        ],
        "links": [
            {"source": "n1", "target": "n2", "relation": "calls-into",
             "confidence": "EXTRACTED", "confidence_score": 1.0,
             "context": "call", "source_file": "a.py",
             "source_location": "L12", "weight": 2},
        ],
        "hyperedges": [{"id": "he1", "nodes": ["n1", "n2"], "label": "shared"}],
        "built_at_commit": "abc1234",
    }


# --- pure round-trip ---

def test_round_trip_exact():
    data = _sample_data()
    node_groups = _node_rows(data, "main")
    edge_groups = _edge_rows(data, "main")
    node_recs = [row["props"] for rows in node_groups.values() for row in rows]
    uid_to_id = {row["props"]["uid"]: row["props"]["id"]
                 for rows in node_groups.values() for row in rows}
    edge_recs = [
        {"source": uid_to_id[row["src"]], "target": uid_to_id[row["tgt"]],
         "props": row["props"]}
        for rows in edge_groups.values() for row in rows
    ]
    meta = {
        "hyperedges_json": json.dumps(data["hyperedges"]),
        "graph_attrs_json": json.dumps(data["graph"]),
        "built_at_commit": data["built_at_commit"],
    }
    rebuilt = _graph_data_from_records(node_recs, edge_recs, meta)

    assert {n["id"]: n for n in rebuilt["nodes"]} == {n["id"]: n for n in data["nodes"]}
    assert rebuilt["links"] == data["links"]
    assert rebuilt["hyperedges"] == data["hyperedges"]
    assert rebuilt["graph"] == data["graph"]
    assert rebuilt["built_at_commit"] == data["built_at_commit"]
    assert rebuilt["directed"] is False and rebuilt["multigraph"] is False


def test_node_rows_grouping_and_uid():
    data = _sample_data()
    groups = _node_rows(data, "feat/x")
    assert set(groups) == {"Code", "Document"}
    row = groups["Code"][0]
    assert row["uid"] == node_uid("feat/x", "n1") == "feat/x\x00n1"
    props = row["props"]
    assert props["branch"] == "feat/x"
    assert props["file_type"] == "code"          # original string kept
    assert props["kind"] == "function"
    # _origin is underscore-prefixed -> rides in extra_json, not a bare prop
    assert "_origin" not in props
    assert json.loads(props["extra_json"])["_origin"] == "ast"
    # community: None must survive (graph.json distinguishes null from missing)
    doc_props = groups["Document"][0]["props"]
    extra = json.loads(doc_props["extra_json"])
    assert extra["community"] is None
    assert extra["tags"] == ["x", "y"]


def test_edge_rows_keep_original_relation():
    data = _sample_data()
    groups = _edge_rows(data, "main")
    assert set(groups) == {"CALLS_INTO"}
    row = groups["CALLS_INTO"][0]
    assert row["props"]["relation"] == "calls-into"   # original, unsanitized
    assert row["src"] == node_uid("main", "n1")
    assert "source" not in row["props"] and "target" not in row["props"]


def test_sanitizers():
    assert safe_rel("calls into-x") == "CALLS_INTO_X"
    assert safe_rel("") == "RELATED_TO"
    assert safe_label("code") == "code"
    assert safe_label("123bad") == "Entity"
    assert safe_label("c0de-x") == "c0dex"
    assert safe_label("") == "Entity"


# --- compute_delta ---

def test_delta_noop():
    data = _sample_data()
    d = compute_delta(data, data)
    assert d["nodes_upsert"] == [] and d["nodes_remove"] == []
    assert d["edges_upsert"] == [] and d["edges_remove"] == []


def test_delta_add_change_remove_node():
    prior = _sample_data()
    new = _sample_data()
    new["nodes"][0] = dict(new["nodes"][0], label="renamed")     # changed
    new["nodes"].append({"id": "n3", "label": "new", "file_type": "code"})  # added
    del new["nodes"][1]                                          # n2 removed
    new["links"] = []                                            # its edge too
    d = compute_delta(prior, new)
    assert {n["id"] for n in d["nodes_upsert"]} == {"n1", "n3"}
    assert d["nodes_remove"] == ["n2"]
    # the removed edge touches removed node n2 -> covered by DETACH DELETE,
    # no explicit edge removal emitted
    assert d["edges_remove"] == []


def test_delta_edge_relation_change_is_remove_plus_add():
    prior = _sample_data()
    new = _sample_data()
    new["links"][0] = dict(new["links"][0], relation="imports")
    d = compute_delta(prior, new)
    assert d["nodes_upsert"] == []
    assert [l["relation"] for l in d["edges_upsert"]] == ["imports"]
    assert d["edges_remove"] == [("n1", "n2", "CALLS_INTO")]


def test_delta_edge_prop_change_same_type_is_upsert_only():
    prior = _sample_data()
    new = _sample_data()
    new["links"][0] = dict(new["links"][0], weight=5)
    d = compute_delta(prior, new)
    assert len(d["edges_upsert"]) == 1
    assert d["edges_remove"] == []


# --- fake driver ---

class _FakeResult:
    def __init__(self, records):
        self._records = records

    def single(self):
        return self._records[0] if self._records else None

    def __iter__(self):
        return iter(self._records)


class _FakeTx:
    def __init__(self, log, results):
        self._log = log
        self._results = results
        self.committed = False

    def run(self, query, **params):
        self._log.append(("tx", query, params))
        return _FakeResult(self._results.get("tx", []))

    def commit(self):
        self.committed = True
        self._log.append(("commit", "", {}))

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class _FakeSession:
    def __init__(self, log, results):
        self._log = log
        self._results = results
        self.tx_count = 0

    def run(self, query, **params):
        self._log.append(("session", query, params))
        return _FakeResult(self._results.get("session", []))

    def begin_transaction(self):
        self.tx_count += 1
        self._log.append(("begin_tx", "", {}))
        return _FakeTx(self._log, self._results)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class _FakeDriver:
    def __init__(self, log, results):
        self._log = log
        self._results = results

    def session(self, database=None):
        return _FakeSession(self._log, self._results)

    def close(self):
        pass


@pytest.fixture
def fake_neo4j(monkeypatch):
    """Inject a stub ``neo4j`` module; yields the (kind, query, params) log."""
    log: list = []
    results: dict = {}
    mod = types.ModuleType("neo4j")

    class _GraphDatabase:
        @staticmethod
        def driver(uri, auth=None):
            log.append(("driver", uri, {"auth": auth}))
            return _FakeDriver(log, results)

    mod.GraphDatabase = _GraphDatabase
    monkeypatch.setitem(sys.modules, "neo4j", mod)
    return log, results


def _open(branch="main"):
    from graphify.backends.neo4j import Neo4jBackend
    return Neo4jBackend("neo4j://localhost:7687", "neo4j", "s3cret",
                        database="neo4j", branch=branch)


def test_save_full_replace_shape(fake_neo4j):
    log, _results = fake_neo4j
    b = _open()
    counts = b.save_graph_data(_sample_data(), prior=None)
    assert counts["nodes"] == 2 and counts["edges"] == 1

    tx_queries = [q for kind, q, _ in log if kind == "tx"]
    # full replace starts by wiping the branch
    assert "DETACH DELETE" in tx_queries[0] and "branch" in tx_queries[0]
    # node upserts are UNWIND-batched MERGEs on uid
    assert any("UNWIND $rows" in q and "MERGE (n:GraphifyNode {uid: row.uid})" in q
               for q in tx_queries)
    # edge upserts too
    assert any("MERGE (a)-[r:CALLS_INTO]->(b)" in q for q in tx_queries)
    # version bump is the LAST tx statement before commit
    assert "GraphifyMeta" in tx_queries[-1] and "coalesce(m.version, 0) + 1" in tx_queries[-1]
    kinds = [k for k, _, _ in log]
    assert kinds.count("begin_tx") == 1, "one transaction per save"
    assert kinds[-1] == "commit"
    # the password never appears in any query text
    assert all("s3cret" not in q for _, q, _ in log)


def test_save_delta_shape(fake_neo4j):
    log, _results = fake_neo4j
    prior = _sample_data()
    new = _sample_data()
    new["nodes"] = [n for n in new["nodes"] if n["id"] != "n2"]
    new["nodes"].append({"id": "n3", "label": "fresh", "file_type": "code"})
    new["links"] = []
    b = _open()
    counts = b.save_graph_data(new, prior=prior)
    assert counts["nodes_removed"] == 1
    assert counts["nodes_upserted"] == 1  # only n3; n1 unchanged

    tx_queries = [q for kind, q, _ in log if kind == "tx"]
    # delta mode must NOT wipe the whole branch
    assert not any(q.startswith("MATCH (n:GraphifyNode {branch: $branch}) DETACH DELETE")
                   for q in tx_queries)
    assert any("UNWIND $uids" in q and "DETACH DELETE" in q for q in tx_queries)
    assert "coalesce(m.version, 0) + 1" in tx_queries[-1]


def test_save_chunks_large_row_sets(fake_neo4j):
    log, _results = fake_neo4j
    data = {
        "nodes": [{"id": f"n{i}", "label": f"x{i}", "file_type": "code"}
                  for i in range(2500)],
        "links": [], "hyperedges": [], "graph": {},
    }
    b = _open()
    b.save_graph_data(data, prior=None)
    node_batches = [p for k, q, p in log
                    if k == "tx" and "MERGE (n:GraphifyNode" in q]
    assert len(node_batches) == 3  # 2500 rows / 1000 per chunk
    assert all(len(p["rows"]) <= 1000 for p in node_batches)


def test_ensure_schema_statements(fake_neo4j):
    log, _results = fake_neo4j
    b = _open()
    b.ensure_schema()
    queries = [q for k, q, _ in log if k == "session"]
    assert any("graphify_node_uid" in q and "IF NOT EXISTS" in q for q in queries)
    assert any("graphify_node_branch" in q for q in queries)
    assert any("graphify_meta_branch" in q for q in queries)


def test_load_graph_data_none_when_branch_never_written(fake_neo4j):
    _log, results = fake_neo4j
    results["session"] = []  # no GraphifyMeta row
    b = _open()
    assert b.load_graph_data() is None
    assert b.get_version() is None


# --- config / ref parsing ---

def test_is_backend_ref():
    assert is_backend_ref("neo4j://localhost:7687")
    assert is_backend_ref("bolt+s://db.example.com:7687/graphs")
    assert not is_backend_ref("graphify-out/graph.json")
    assert not is_backend_ref("http://localhost:7687")
    assert not is_backend_ref("C:\\progetti\\graph.json")


def test_backend_ref_survives_path_mangling():
    # Callers often wrap CLI args in Path(), which collapses the double slash
    # ("neo4j:/host" on POSIX, "neo4j:\host" on Windows). Both must still be
    # recognized and normalized back to a canonical URI.
    from pathlib import Path
    from graphify.backends import normalize_backend_ref
    mangled = str(Path("neo4j://localhost:7687"))
    assert is_backend_ref(mangled)
    assert normalize_backend_ref(mangled) == "neo4j://localhost:7687"
    cfg = parse_backend_uri(str(Path("bolt+s://db.example.com:7687/g")))
    assert cfg["uri"] == "bolt+s://db.example.com:7687"
    assert cfg["database"] == "g"


def test_parse_backend_uri_database_and_user():
    cfg = parse_backend_uri("neo4j+s://alice@db.example.com:7688/mygraph")
    assert cfg["uri"] == "neo4j+s://db.example.com:7688"
    assert cfg["user"] == "alice"
    assert cfg["database"] == "mygraph"


def test_parse_backend_uri_rejects_password():
    with pytest.raises(ValueError, match="NEO4J_PASSWORD"):
        parse_backend_uri("neo4j://alice:hunter2@localhost:7687")


def test_backend_config_env_overrides_file(tmp_path, monkeypatch):
    (tmp_path / "backend.json").write_text(json.dumps(
        {"backend": "neo4j", "uri": "neo4j://filehost:7687"}), encoding="utf-8")
    monkeypatch.setenv("GRAPHIFY_NEO4J_URI", "neo4j://envhost:7687")
    cfg = backend_config(tmp_path)
    assert cfg["uri"] == "neo4j://envhost:7687"
    monkeypatch.delenv("GRAPHIFY_NEO4J_URI")
    cfg = backend_config(tmp_path)
    assert cfg["uri"] == "neo4j://filehost:7687"
    assert cfg["user"] == "neo4j"


def test_backend_config_absent_or_invalid(tmp_path, monkeypatch):
    monkeypatch.delenv("GRAPHIFY_NEO4J_URI", raising=False)
    assert backend_config(tmp_path) is None
    (tmp_path / "backend.json").write_text("{not json", encoding="utf-8")
    assert backend_config(tmp_path) is None
    (tmp_path / "backend.json").write_text(json.dumps({"backend": "other"}), encoding="utf-8")
    assert backend_config(tmp_path) is None


# --- current_branch ---

def test_current_branch_env_override(monkeypatch):
    monkeypatch.setenv("GRAPHIFY_BRANCH", "ci-run")
    assert current_branch() == "ci-run"


def test_current_branch_detached_and_fallback(monkeypatch):
    monkeypatch.delenv("GRAPHIFY_BRANCH", raising=False)
    import graphify.backends as backends

    calls = {"n": 0}

    def fake_run(cmd, **kw):
        calls["n"] += 1
        out = "HEAD" if calls["n"] == 1 else "abc123"
        return types.SimpleNamespace(returncode=0, stdout=out + "\n", stderr="")

    monkeypatch.setattr(backends.subprocess, "run", fake_run)
    assert current_branch() == "detached-abc123"

    def failing_run(cmd, **kw):
        raise OSError("git not found")

    monkeypatch.setattr(backends.subprocess, "run", failing_run)
    assert current_branch() == "_default"


# --- serve: TTL/version polling ---

class _FakeServeBackend:
    def __init__(self):
        self.version = 1
        self.branch = "_default"
        self.fail = False
        self.version_calls = 0
        self.load_calls = 0
        self.data = {"directed": False, "multigraph": False, "graph": {},
                     "nodes": [{"id": "a", "label": "A", "community": 0}],
                     "links": [], "hyperedges": []}

    def get_version(self):
        self.version_calls += 1
        if self.fail:
            raise RuntimeError("connection refused")
        return self.version

    def load_graph_data(self):
        self.load_calls += 1
        return dict(self.data)


@pytest.fixture
def serve_ctx(monkeypatch):
    import threading
    import graphify.backends as backends
    fb = _FakeServeBackend()
    monkeypatch.setattr(backends, "open_backend", lambda *a, **kw: fb)
    monkeypatch.setattr(backends, "current_branch", lambda repo_root=None: "main")
    cache: dict = {}
    lock = threading.Lock()

    def load(ttl=60.0):
        from graphify.serve import _load_backend_ctx
        return _load_backend_ctx("neo4j://host:7687", cfg=None, out_dir=None,
                                 cache=cache, lock=lock, ttl=ttl)

    return fb, cache, load


def _expire(cache):
    cache["neo4j://host:7687"]["last_poll"] -= 10_000


def test_serve_no_poll_within_ttl(serve_ctx):
    fb, cache, load = serve_ctx
    G1, comm = load()
    assert fb.load_calls == 1 and fb.version_calls == 1
    assert comm == {0: ["a"]}
    G2, _ = load()
    assert G2 is G1
    assert fb.version_calls == 1, "within TTL the DB must not be touched"


def test_serve_version_poll_without_reload(serve_ctx):
    fb, cache, load = serve_ctx
    G1, _ = load()
    _expire(cache)
    G2, _ = load()
    assert fb.version_calls == 2, "expired TTL polls the version"
    assert fb.load_calls == 1 and G2 is G1, "unchanged version must not reload"


def test_serve_reload_on_version_bump(serve_ctx):
    fb, cache, load = serve_ctx
    G1, _ = load()
    fb.version = 2
    fb.data["nodes"].append({"id": "b", "label": "B", "community": 0})
    _expire(cache)
    G2, comm = load()
    assert fb.load_calls == 2 and G2 is not G1
    assert "b" in G2


def test_serve_poll_failure_serves_cached(serve_ctx, capsys):
    fb, cache, load = serve_ctx
    G1, _ = load()
    fb.fail = True
    _expire(cache)
    G2, _ = load()
    assert G2 is G1
    assert "serving cached graph" in capsys.readouterr().err
    # and the failed poll refreshed last_poll, so the next call within TTL
    # doesn't hammer the dead DB
    load()
    assert fb.version_calls == 2


def test_serve_branch_never_written(serve_ctx):
    fb, cache, load = serve_ctx
    fb.version = None
    with pytest.raises(FileNotFoundError, match="run a build first"):
        load()


# --- BackendSync (materialize-before / push-after bracket) ---

class _FakeSyncBackend:
    def __init__(self, version=None, data=None):
        self.version = version
        self.branch = "main"
        self.data = data
        self.save_calls: list = []
        self.load_calls = 0
        self.fail_save = False
        self.closed = False

    def get_version(self):
        return self.version

    def load_graph_data(self):
        self.load_calls += 1
        return self.data

    def save_graph_data(self, data, *, prior=None):
        if self.fail_save:
            raise RuntimeError("boom")
        self.save_calls.append((data, prior))
        self.version = (self.version or 0) + 1
        return {"nodes": len(data.get("nodes", [])), "edges": len(data.get("links", [])),
                "nodes_upserted": 0, "edges_upserted": 0,
                "nodes_removed": 0, "edges_removed": 0}

    def close(self):
        self.closed = True


@pytest.fixture
def sync_env(tmp_path, monkeypatch):
    import graphify.backends as backends
    out = tmp_path / "graphify-out"
    out.mkdir()
    (out / "backend.json").write_text(json.dumps(
        {"backend": "neo4j", "uri": "neo4j://h:7687"}), encoding="utf-8")
    monkeypatch.delenv("GRAPHIFY_NEO4J_URI", raising=False)
    fb = _FakeSyncBackend()
    monkeypatch.setattr(backends, "open_backend", lambda *a, **kw: fb)
    monkeypatch.setattr(backends, "current_branch", lambda repo_root=None: "main")
    return out, fb


def _local_graph(out, data):
    (out / "graph.json").write_text(json.dumps(data), encoding="utf-8")


def test_sync_disabled_without_config(tmp_path, monkeypatch):
    monkeypatch.delenv("GRAPHIFY_NEO4J_URI", raising=False)
    from graphify.backends import BackendSync
    sync = BackendSync(tmp_path)
    assert not sync.enabled
    assert sync.prepare() is True
    sync.push()  # no-op, must not raise
    sync.close()


def test_sync_prepare_fail_closed(sync_env, monkeypatch, capsys):
    import graphify.backends as backends
    out, _fb = sync_env

    def boom(*a, **kw):
        raise RuntimeError("connection refused")

    monkeypatch.setattr(backends, "open_backend", boom)
    from graphify.backends import BackendSync
    sync = BackendSync(out)
    assert sync.prepare() is False
    assert "graphify backend unset" in capsys.readouterr().err


def test_sync_first_run_seeds_branch(sync_env):
    out, fb = sync_env  # fb.version None -> branch never written
    from graphify.backends import BackendSync, load_backend_state
    sync = BackendSync(out)
    assert sync.prepare() is True
    assert sync.prior is None
    data = _sample_data()
    _local_graph(out, data)
    sync.push(changed=False)  # even a no-change rebuild seeds an empty branch
    assert len(fb.save_calls) == 1
    assert fb.save_calls[0][1] is None  # full replace
    assert load_backend_state(out)["version"] == fb.version


def test_sync_in_sync_skips_materialize_and_pull(sync_env):
    out, fb = sync_env
    fb.version = 3
    fb.data = _sample_data()
    local = _sample_data()
    _local_graph(out, local)
    from graphify.backends import BackendSync, save_backend_state
    save_backend_state(out, version=3)
    sync = BackendSync(out)
    assert sync.prepare() is True
    assert fb.load_calls == 0, "in-sync must not pull the graph from the DB"
    assert sync.prior == local
    sync.push(changed=False)
    assert fb.save_calls == [], "in sync + unchanged -> nothing to push"


def test_sync_external_change_rematerializes(sync_env):
    out, fb = sync_env
    fb.version = 5
    fb.data = _sample_data()
    _local_graph(out, {"nodes": [], "links": []})  # stale local cache
    from graphify.backends import BackendSync, save_backend_state
    save_backend_state(out, version=3)  # we last synced v3; DB moved to v5
    sync = BackendSync(out)
    assert sync.prepare() is True
    assert fb.load_calls == 1
    refreshed = json.loads((out / "graph.json").read_text(encoding="utf-8"))
    assert {n["id"] for n in refreshed["nodes"]} == {"n1", "n2"}
    assert (out / ".gitignore").exists(), "materialize drops the self-ignore file"


def test_sync_push_failure_marks_dirty_then_recovers(sync_env, capsys):
    out, fb = sync_env
    fb.version = 2
    fb.data = _sample_data()
    _local_graph(out, _sample_data())
    from graphify.backends import BackendSync, load_backend_state, save_backend_state
    save_backend_state(out, version=2)
    sync = BackendSync(out)
    assert sync.prepare() is True
    fb.fail_save = True
    sync.push(changed=True)
    err = capsys.readouterr().err
    assert "BEHIND" in err
    state = load_backend_state(out)
    assert state.get("dirty") is True and state["version"] == 2

    # next run: same DB version but dirty -> diff against the true DB state
    # and push even though the rebuild reports no local change
    fb.fail_save = False
    sync2 = BackendSync(out)
    assert sync2.prepare() is True
    assert fb.load_calls == 1, "dirty state pulls the DB as the delta base"
    sync2.push(changed=False)
    assert len(fb.save_calls) == 1
    assert not load_backend_state(out).get("dirty")


# --- .gitignore self-ignore ---

def test_ensure_out_gitignore_idempotent(tmp_path):
    out = tmp_path / "graphify-out"
    ensure_out_gitignore(out)
    gi = out / ".gitignore"
    assert gi.read_text(encoding="utf-8").splitlines()[-1] == "*"
    gi.write_text("custom\n", encoding="utf-8")
    ensure_out_gitignore(out)  # never overwrites
    assert gi.read_text(encoding="utf-8") == "custom\n"
