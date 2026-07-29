"""Neo4j graph backend — Neo4j as an opt-in source of truth, not just an export sink.

Unlike ``graphify.exporters.graphdb.push_to_neo4j`` (a one-way, lossy export kept
for back-compat), this backend round-trips the full graph.json node-link dict:

  - original ``relation``/``file_type`` strings are stored as properties (the
    sanitized forms appear only as Cypher labels/relationship types, which are
    not parameterizable and must be injection-safe);
  - underscore-prefixed, ``None``-valued and non-scalar attributes survive via a
    per-node/per-edge ``extra_json`` property, so ``load_graph_data`` returns a
    dict equal to what ``save_graph_data`` received;
  - hyperedges, ``built_at_commit`` and the top-level ``graph`` attribute dict
    ride on a per-branch ``:GraphifyMeta`` node;
  - every node/edge is tagged with a git ``branch`` so multiple branches coexist
    in one database (community edition = one DB). The uniqueness key is
    ``uid = f"{branch}\\x00{id}"`` — a composite property, because composite
    node-key constraints are Enterprise-only.

Write safety: each ``save_graph_data`` call runs in ONE explicit transaction and
bumps ``GraphifyMeta.version`` as the LAST statement, so a reader that keys its
cache on the version can never observe a torn graph.
"""
from __future__ import annotations

import json
import os
import re
import sys
from datetime import datetime, timezone

SCHEMA_VERSION = 1

# Rows per UNWIND statement. One UNWIND per label/rel-type group (labels and
# relationship types cannot be parameterized in Cypher), chunked so a huge graph
# does not ship one giant parameter list.
_CHUNK = 1000

# Keys that are structural in the node-link dict, not node/edge properties.
_EDGE_STRUCTURAL_KEYS = ("source", "target")


def _require_driver():
    try:
        from neo4j import GraphDatabase
    except ImportError as e:
        raise ImportError(
            'neo4j driver not installed. Run: pip install "graphifyy[neo4j]" (or pip install neo4j)'
        ) from e
    return GraphDatabase


def safe_rel(relation: str) -> str:
    """Sanitize a relationship type (Cypher rel types are not parameterizable)."""
    return re.sub(r"[^A-Z0-9_]", "_", str(relation).upper().replace(" ", "_").replace("-", "_")) or "RELATED_TO"


def safe_label(label: str) -> str:
    """Sanitize a node label to prevent Cypher injection."""
    sanitized = re.sub(r"[^A-Za-z0-9_]", "", str(label))
    if not sanitized or not sanitized[0].isalpha():
        return "Entity"
    return sanitized


def node_uid(branch: str, node_id: str) -> str:
    # \x00 cannot appear in a git branch name or a graphify node id, so the
    # composite key is unambiguous.
    return f"{branch}\x00{node_id}"


def _split_props(attrs: dict) -> tuple[dict, str | None]:
    """Split attributes into Neo4j-storable scalars and an extra_json remainder.

    Scalars (str/int/float/bool) become native properties. Everything else —
    underscore-prefixed keys (e.g. ``_origin``), ``None`` values (Neo4j drops
    null properties, but graph.json distinguishes ``community: null`` from a
    missing key), lists/dicts — is JSON-encoded so the read path can restore the
    input dict exactly.
    """
    props: dict = {}
    extra: dict = {}
    for k, v in attrs.items():
        if k.startswith("_") or v is None or not isinstance(v, (str, int, float, bool)):
            extra[k] = v
        else:
            props[k] = v
    return props, (json.dumps(extra, ensure_ascii=False) if extra else None)


def _merge_props(props: dict) -> dict:
    """Inverse of :func:`_split_props`: fold extra_json back into the dict."""
    out = dict(props)
    extra_json = out.pop("extra_json", None)
    if extra_json:
        out.update(json.loads(extra_json))
    return out


def _node_rows(data: dict, branch: str) -> dict[str, list[dict]]:
    """graph.json nodes -> UNWIND rows grouped by sanitized secondary label."""
    groups: dict[str, list[dict]] = {}
    for node in data.get("nodes", []):
        node_id = node["id"]
        attrs = {k: v for k, v in node.items() if k != "id"}
        props, extra_json = _split_props(attrs)
        props["id"] = node_id
        props["branch"] = branch
        props["uid"] = node_uid(branch, node_id)
        if extra_json is not None:
            props["extra_json"] = extra_json
        label = safe_label(str(node.get("file_type", "Entity")).capitalize())
        groups.setdefault(label, []).append({"uid": props["uid"], "props": props})
    return groups


def _edge_rows(data: dict, branch: str) -> dict[str, list[dict]]:
    """graph.json links -> UNWIND rows grouped by sanitized relationship type."""
    links = data.get("links", data.get("edges", []))
    groups: dict[str, list[dict]] = {}
    for link in links:
        attrs = {k: v for k, v in link.items() if k not in _EDGE_STRUCTURAL_KEYS}
        props, extra_json = _split_props(attrs)
        props["branch"] = branch
        if extra_json is not None:
            props["extra_json"] = extra_json
        rel = safe_rel(link.get("relation", "RELATED_TO"))
        groups.setdefault(rel, []).append({
            "src": node_uid(branch, link["source"]),
            "tgt": node_uid(branch, link["target"]),
            "props": props,
        })
    return groups


def _graph_data_from_records(node_recs, edge_recs, meta: dict) -> dict:
    """Inverse of the row builders: DB records -> graph.json-shaped dict.

    ``node_recs`` is an iterable of property dicts; ``edge_recs`` an iterable of
    ``{"source": id, "target": id, "props": {...}}``; ``meta`` the GraphifyMeta
    property dict.
    """
    nodes = []
    for props in node_recs:
        merged = _merge_props(props)
        merged.pop("uid", None)
        merged.pop("branch", None)
        nodes.append(merged)
    links = []
    for rec in edge_recs:
        props = _merge_props(rec["props"])
        props.pop("branch", None)
        link = dict(props)
        link["source"] = rec["source"]
        link["target"] = rec["target"]
        links.append(link)
    data = {
        "directed": False,
        "multigraph": False,
        "graph": json.loads(meta.get("graph_attrs_json") or "{}"),
        "nodes": nodes,
        "links": links,
        "hyperedges": json.loads(meta.get("hyperedges_json") or "[]"),
    }
    if meta.get("built_at_commit"):
        data["built_at_commit"] = meta["built_at_commit"]
    return data


def _edge_key(link: dict) -> tuple:
    return (link["source"], link["target"], safe_rel(link.get("relation", "RELATED_TO")))


def compute_delta(prior: dict, new: dict) -> dict:
    """Diff two graph.json dicts into the minimal upsert/remove sets.

    Nodes are keyed by id; edges by (source, target, sanitized rel type) — the
    same key the DB enforces, so a relation string change that lands on a new
    sanitized type becomes remove-old + add-new.
    """
    prior_nodes = {n["id"]: n for n in prior.get("nodes", [])}
    new_nodes = {n["id"]: n for n in new.get("nodes", [])}
    nodes_upsert = [n for nid, n in new_nodes.items()
                    if nid not in prior_nodes or prior_nodes[nid] != n]
    nodes_remove = [nid for nid in prior_nodes if nid not in new_nodes]

    prior_links = {_edge_key(l): l for l in prior.get("links", prior.get("edges", []))}
    new_links = {_edge_key(l): l for l in new.get("links", new.get("edges", []))}
    edges_upsert = [l for k, l in new_links.items()
                    if k not in prior_links or prior_links[k] != l]
    removed_node_set = set(nodes_remove)
    # DETACH DELETE on removed nodes already drops their edges; skip explicit
    # edge removals that touch a removed endpoint.
    edges_remove = [k for k in prior_links
                    if k not in new_links
                    and k[0] not in removed_node_set and k[1] not in removed_node_set]
    return {
        "nodes_upsert": nodes_upsert,
        "nodes_remove": nodes_remove,
        "edges_upsert": edges_upsert,
        "edges_remove": edges_remove,
    }


def _chunks(rows: list, size: int = _CHUNK):
    for i in range(0, len(rows), size):
        yield rows[i:i + size]


# Pushes below this many rows stay silent — a routine incremental delta must
# not add noise to every `graphify update`.
_PROGRESS_MIN_ROWS = 2000


class _PushProgress:
    """Progress reporting for large pushes, on stderr.

    On a TTY the same line is rewritten in place (``\\r``); otherwise (hooks,
    CI logs) a plain line is emitted every ~10k rows so the log still shows
    the push is alive without flooding it.
    """

    def __init__(self, total_rows: int, *, branch: str, mode: str):
        self.total = total_rows
        self.done = 0
        self._last_logged = 0
        self.enabled = total_rows >= _PROGRESS_MIN_ROWS
        self._tty = self.enabled and sys.stderr.isatty()
        if self.enabled:
            print(f"[graphify] Neo4j: pushing {total_rows:_d} rows to branch "
                  f"{branch!r} ({mode})...", file=sys.stderr, flush=True)

    def _line(self) -> str:
        pct = self.done * 100 // self.total if self.total else 100
        return f"[graphify] Neo4j push: {self.done:_d}/{self.total:_d} rows ({pct}%)"

    def advance(self, nrows: int) -> None:
        if not self.enabled or not nrows:
            return
        self.done += nrows
        if self._tty:
            print("\r" + self._line(), end="", file=sys.stderr, flush=True)
        elif self.done - self._last_logged >= 10_000:
            self._last_logged = self.done
            print(self._line(), file=sys.stderr, flush=True)

    def finish(self) -> None:
        if not self.enabled:
            return
        if self._tty:
            print("\r" + self._line(), file=sys.stderr, flush=True)
        elif self.done != self._last_logged:
            print(self._line(), file=sys.stderr, flush=True)


class Neo4jBackend:
    """Branch-scoped Neo4j storage for a graphify graph."""

    def __init__(self, uri: str, user: str, password: str, *,
                 database: str = "neo4j", branch: str = "_default"):
        GraphDatabase = _require_driver()
        self.uri = uri
        self.database = database
        self.branch = branch
        self._driver = GraphDatabase.driver(uri, auth=(user, password))

    # -- lifecycle ---------------------------------------------------------
    def close(self) -> None:
        self._driver.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def _session(self):
        return self._driver.session(database=self.database)

    # -- schema ------------------------------------------------------------
    def ensure_schema(self) -> None:
        """Idempotent constraints/indexes. Schema ops cannot share a transaction
        with data writes, so this runs in its own auto-commit session."""
        statements = (
            "CREATE CONSTRAINT graphify_node_uid IF NOT EXISTS "
            "FOR (n:GraphifyNode) REQUIRE n.uid IS UNIQUE",
            "CREATE INDEX graphify_node_branch IF NOT EXISTS "
            "FOR (n:GraphifyNode) ON (n.branch)",
            "CREATE CONSTRAINT graphify_meta_branch IF NOT EXISTS "
            "FOR (m:GraphifyMeta) REQUIRE m.branch IS UNIQUE",
        )
        with self._session() as session:
            for stmt in statements:
                session.run(stmt)

    # -- meta --------------------------------------------------------------
    def get_version(self) -> int | None:
        """Current write-version of this branch, or None if never written."""
        with self._session() as session:
            rec = session.run(
                "MATCH (m:GraphifyMeta {branch: $branch}) RETURN m.version AS v",
                branch=self.branch,
            ).single()
        return rec["v"] if rec else None

    def get_meta(self) -> dict | None:
        with self._session() as session:
            rec = session.run(
                "MATCH (m:GraphifyMeta {branch: $branch}) RETURN properties(m) AS p",
                branch=self.branch,
            ).single()
        return dict(rec["p"]) if rec else None

    # -- read --------------------------------------------------------------
    def load_graph_data(self) -> dict | None:
        """The branch's graph as a graph.json-shaped dict, or None when the
        branch has never been written (no GraphifyMeta node)."""
        with self._session() as session:
            meta_rec = session.run(
                "MATCH (m:GraphifyMeta {branch: $branch}) RETURN properties(m) AS p",
                branch=self.branch,
            ).single()
            if meta_rec is None:
                return None
            # DoS guard, the DB analogue of security.check_graph_file_size_cap:
            # refuse to materialize a graph whose declared node count implies a
            # payload past the configured byte cap (~1 KiB/node heuristic),
            # BEFORE pulling every node over the wire.
            node_count = dict(meta_rec["p"]).get("node_count") or 0
            from graphify.security import _max_graph_file_bytes
            cap_nodes = max(1, _max_graph_file_bytes() // 1024)
            if node_count > cap_nodes:
                raise ValueError(
                    f"graph for branch {self.branch!r} has {node_count:_d} nodes, "
                    f"exceeds the {cap_nodes:_d}-node cap (derived from the graph "
                    f"byte cap; set GRAPHIFY_MAX_GRAPH_BYTES to raise it)"
                )
            if node_count >= _PROGRESS_MIN_ROWS:
                print(f"[graphify] Neo4j: loading {node_count:_d} nodes from "
                      f"branch {self.branch!r}...", file=sys.stderr, flush=True)
            node_recs = [dict(r["p"]) for r in session.run(
                "MATCH (n:GraphifyNode {branch: $branch}) RETURN properties(n) AS p",
                branch=self.branch,
            )]
            # Untyped -[r]-> match keeps loading independent of the dynamic
            # relationship types the writer generated.
            edge_recs = [
                {"source": r["source"], "target": r["target"], "props": dict(r["p"])}
                for r in session.run(
                    "MATCH (a:GraphifyNode {branch: $branch})-[r]->"
                    "(b:GraphifyNode {branch: $branch}) "
                    "RETURN a.id AS source, b.id AS target, properties(r) AS p",
                    branch=self.branch,
                )
            ]
        return _graph_data_from_records(node_recs, edge_recs, dict(meta_rec["p"]))

    # -- write -------------------------------------------------------------
    def save_graph_data(self, data: dict, *, prior: dict | None = None) -> dict[str, int]:
        """Persist ``data`` for this branch.

        ``prior=None`` -> authoritative full replace (delete branch + insert).
        ``prior=dict`` -> minimal delta computed dict-vs-dict.

        By default everything runs in ONE transaction. Set
        ``GRAPHIFY_NEO4J_TX_ROWS=<n>`` to commit every ~n rows instead — easier
        on the Neo4j heap for very large seeds. Reader consistency does not
        depend on the single transaction: the ``GraphifyMeta.version`` bump is
        ALWAYS the last statement of the last transaction, and version-keyed
        readers (the MCP server) reload only on a version change. The chunked
        mode's residual risk — a crash leaving the branch partially written —
        is covered by the caller (``BackendSync``) marking its sync state
        dirty before the push, so the next run diffs against the DB's real
        content and repairs it.

        Pushes bigger than ~2000 rows report progress on stderr.
        """
        branch = self.branch
        if prior is None:
            delta = None
            node_groups = _node_rows(data, branch)
            edge_groups = _edge_rows(data, branch)
            nodes_removed: list[str] = []
            edge_removals: dict[str, list[dict]] = {}
        else:
            delta = compute_delta(prior, data)
            node_groups = _node_rows({"nodes": delta["nodes_upsert"]}, branch)
            edge_groups = _edge_rows({"links": delta["edges_upsert"]}, branch)
            nodes_removed = delta["nodes_remove"]
            edge_removals = {}
            for src, tgt, rel in delta["edges_remove"]:
                edge_removals.setdefault(rel, []).append({
                    "src": node_uid(branch, src),
                    "tgt": node_uid(branch, tgt),
                })

        links = data.get("links", data.get("edges", []))
        counts = {
            "nodes": len(data.get("nodes", [])),
            "edges": len(links),
            "nodes_upserted": sum(len(rows) for rows in node_groups.values()),
            "edges_upserted": sum(len(rows) for rows in edge_groups.values()),
            "nodes_removed": len(nodes_removed),
            "edges_removed": sum(len(rows) for rows in edge_removals.values()),
        }

        if delta is not None and not (counts["nodes_upserted"] or counts["edges_upserted"]
                                      or counts["nodes_removed"] or counts["edges_removed"]):
            # Empty delta: leave the DB (and its version) untouched so pollers
            # keyed on the version don't reload an identical graph.
            return counts

        meta_props = {
            "branch": branch,
            "schema_version": SCHEMA_VERSION,
            "updated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "node_count": counts["nodes"],
            "edge_count": counts["edges"],
            "hyperedges_json": json.dumps(data.get("hyperedges", []), ensure_ascii=False),
            "graph_attrs_json": json.dumps(data.get("graph", {}), ensure_ascii=False),
            "built_at_commit": data.get("built_at_commit") or "",
        }

        # Assemble every statement as (query, params, row_count) so the
        # executor below can run them atomically or in chunked transactions
        # and report progress either way. Removals come first (explicit edge
        # deletes, then DETACH DELETE of removed nodes), then node upserts
        # (so the edge MATCHes can bind), then edge upserts.
        ops: list[tuple[str, dict, int]] = []
        if delta is None:
            ops.append((
                "MATCH (n:GraphifyNode {branch: $branch}) DETACH DELETE n",
                {"branch": branch}, 0,
            ))
        else:
            for rel, rows in edge_removals.items():
                for chunk in _chunks(rows):
                    ops.append((
                        "UNWIND $rows AS row "
                        "MATCH (a:GraphifyNode {uid: row.src})"
                        f"-[r:{rel}]->"
                        "(b:GraphifyNode {uid: row.tgt}) DELETE r",
                        {"rows": chunk}, len(chunk),
                    ))
            if nodes_removed:
                uids = [node_uid(branch, nid) for nid in nodes_removed]
                for chunk in _chunks(uids):
                    ops.append((
                        "UNWIND $uids AS uid "
                        "MATCH (n:GraphifyNode {uid: uid}) DETACH DELETE n",
                        {"uids": chunk}, len(chunk),
                    ))
        for label, rows in node_groups.items():
            for chunk in _chunks(rows):
                ops.append((
                    "UNWIND $rows AS row "
                    "MERGE (n:GraphifyNode {uid: row.uid}) "
                    f"SET n = row.props SET n:{label}",
                    {"rows": chunk}, len(chunk),
                ))
        for rel, rows in edge_groups.items():
            for chunk in _chunks(rows):
                ops.append((
                    "UNWIND $rows AS row "
                    "MATCH (a:GraphifyNode {uid: row.src}), "
                    "(b:GraphifyNode {uid: row.tgt}) "
                    f"MERGE (a)-[r:{rel}]->(b) SET r = row.props",
                    {"rows": chunk}, len(chunk),
                ))

        try:
            tx_rows = int(os.environ.get("GRAPHIFY_NEO4J_TX_ROWS", "0") or 0)
        except ValueError:
            tx_rows = 0
        total_rows = sum(n for _, _, n in ops)
        progress = _PushProgress(total_rows, branch=branch,
                                 mode=f"chunks of {tx_rows}" if tx_rows > 0 else "atomic")

        with self._session() as session:
            tx = session.begin_transaction()
            rows_in_tx = 0
            try:
                for query, params, nrows in ops:
                    tx.run(query, **params)
                    rows_in_tx += nrows
                    progress.advance(nrows)
                    if tx_rows > 0 and rows_in_tx >= tx_rows:
                        tx.commit()
                        tx = session.begin_transaction()
                        rows_in_tx = 0
                # Version bump LAST — always in the FINAL transaction, so a
                # version-keyed reader reloads only once everything above is
                # committed, in both atomic and chunked modes.
                tx.run(
                    "MERGE (m:GraphifyMeta {branch: $branch}) "
                    "SET m += $props, m.version = coalesce(m.version, 0) + 1",
                    branch=branch,
                    props=meta_props,
                )
                tx.commit()
            finally:
                progress.finish()
        return counts

    # -- branches ----------------------------------------------------------
    def list_branches(self) -> list[dict]:
        with self._session() as session:
            return [dict(r["p"]) for r in session.run(
                "MATCH (m:GraphifyMeta) RETURN properties(m) AS p ORDER BY m.branch",
            )]

    def delete_branch(self, branch: str) -> dict[str, int]:
        with self._session() as session:
            with session.begin_transaction() as tx:
                rec = tx.run(
                    "MATCH (n:GraphifyNode {branch: $branch}) "
                    "DETACH DELETE n RETURN count(n) AS c",
                    branch=branch,
                ).single()
                meta_rec = tx.run(
                    "MATCH (m:GraphifyMeta {branch: $branch}) "
                    "DELETE m RETURN count(m) AS c",
                    branch=branch,
                ).single()
                tx.commit()
        return {
            "nodes": rec["c"] if rec else 0,
            "meta": meta_rec["c"] if meta_rec else 0,
        }
