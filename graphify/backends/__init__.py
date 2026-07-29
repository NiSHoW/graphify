"""Pluggable graph storage backends (opt-in; default file mode is unchanged).

A project opts into the Neo4j backend either with the ``GRAPHIFY_NEO4J_URI``
env var or a ``backend.json`` next to graph.json in the output dir (written by
``graphify backend set``). Passwords are NEVER stored in backend.json nor read
from URI userinfo — only the ``GRAPHIFY_NEO4J_PASSWORD``/``NEO4J_PASSWORD`` env
vars (same F-031 rationale as ``export neo4j --push``: keep secrets off argv
and out of version-controllable files).

This module stays importable without the neo4j driver installed: the driver is
touched only inside :func:`open_backend`.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path
from urllib.parse import urlparse, urlunparse

NEO4J_SCHEMES = ("neo4j", "neo4j+s", "neo4j+ssc", "bolt", "bolt+s", "bolt+ssc")

# Matches a backend URI even after Path() mangled it: callers often wrap CLI
# arguments in Path(), which collapses "neo4j://host" to "neo4j:/host" (POSIX)
# or "neo4j:\host" (Windows). Accept 1-2 slashes of either kind after the
# scheme; normalize_backend_ref() restores the canonical "scheme://" form.
_REF_RE = re.compile(
    r"^(neo4j|neo4j\+s|neo4j\+ssc|bolt|bolt\+s|bolt\+ssc):[/\\]{1,2}",
    re.IGNORECASE,
)

BACKEND_CONFIG_NAME = "backend.json"
_STATE_NAME = ".graphify_backend_state.json"

# Self-ignoring output dir (node_modules-style): the folder carries its own
# .gitignore so the materialized graph.json cache never needs a repo-root
# .gitignore entry. Only created in backend mode — in file mode some teams
# commit graph.json on purpose.
_GITIGNORE_CONTENT = "# graphify: this directory is a local cache of the Neo4j graph\n*\n"


def is_backend_ref(ref) -> bool:
    """True when ``ref`` is a backend URI (neo4j://...) rather than a file path.
    Tolerates Path()-mangled forms (single slash, backslashes)."""
    return bool(_REF_RE.match(str(ref)))


def normalize_backend_ref(ref) -> str:
    """Restore the canonical ``scheme://`` form of a (possibly Path-mangled)
    backend URI. No-op for an already-canonical URI."""
    s = str(ref).replace("\\", "/")
    scheme, _, rest = s.partition(":")
    return f"{scheme}://{rest.lstrip('/')}"


def parse_backend_uri(uri: str) -> dict:
    """Split a ``neo4j://[user@]host[:port][/database]`` ref into driver config.

    The database rides in the URI path (the bolt driver has no database in the
    URI proper); userinfo may carry a username. A password embedded in the URI
    is rejected — it would leak via argv/shell history (F-031).
    """
    parsed = urlparse(normalize_backend_ref(uri))
    if parsed.password:
        raise ValueError(
            "password in the URI is not supported; set NEO4J_PASSWORD "
            "(or GRAPHIFY_NEO4J_PASSWORD) instead"
        )
    database = (parsed.path or "").lstrip("/") or "neo4j"
    netloc = parsed.hostname or "localhost"
    if parsed.port:
        netloc = f"{netloc}:{parsed.port}"
    driver_uri = urlunparse((parsed.scheme, netloc, "", "", "", ""))
    return {
        "backend": "neo4j",
        "uri": driver_uri,
        "user": parsed.username or "neo4j",
        "database": database,
    }


def backend_config(out_dir: "Path | str | None" = None) -> dict | None:
    """Resolve the backend configuration, or None for default file mode.

    Order: 1) ``GRAPHIFY_NEO4J_URI`` env (+ ``GRAPHIFY_NEO4J_USER`` /
    ``GRAPHIFY_NEO4J_DATABASE`` overrides); 2) ``<out_dir>/backend.json``.
    """
    env_uri = os.environ.get("GRAPHIFY_NEO4J_URI")
    if env_uri:
        cfg = parse_backend_uri(env_uri)
        if os.environ.get("GRAPHIFY_NEO4J_USER"):
            cfg["user"] = os.environ["GRAPHIFY_NEO4J_USER"]
        if os.environ.get("GRAPHIFY_NEO4J_DATABASE"):
            cfg["database"] = os.environ["GRAPHIFY_NEO4J_DATABASE"]
        return cfg
    if out_dir is None:
        from graphify.paths import GRAPHIFY_OUT
        out_dir = GRAPHIFY_OUT
    cfg_path = Path(out_dir) / BACKEND_CONFIG_NAME
    try:
        raw = json.loads(cfg_path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return None
    if not isinstance(raw, dict) or raw.get("backend") != "neo4j" or not raw.get("uri"):
        return None
    return {
        "backend": "neo4j",
        "uri": str(raw["uri"]),
        "user": str(raw.get("user") or "neo4j"),
        "database": str(raw.get("database") or "neo4j"),
    }


def _resolve_password() -> str:
    password = os.environ.get("GRAPHIFY_NEO4J_PASSWORD") or os.environ.get("NEO4J_PASSWORD")
    if not password:
        raise ValueError(
            "Neo4j backend configured but no password found: set NEO4J_PASSWORD "
            "(or GRAPHIFY_NEO4J_PASSWORD) in the environment"
        )
    return password


def open_backend(ref_or_cfg=None, *, branch: str | None = None,
                 out_dir: "Path | str | None" = None):
    """Open a :class:`~graphify.backends.neo4j.Neo4jBackend`.

    ``ref_or_cfg`` may be a ``neo4j://`` URI string, a config dict (as returned
    by :func:`backend_config`), or None to resolve from env/backend.json.
    """
    if isinstance(ref_or_cfg, str):
        cfg = parse_backend_uri(ref_or_cfg)
    elif isinstance(ref_or_cfg, dict):
        cfg = ref_or_cfg
    else:
        cfg = backend_config(out_dir)
        if cfg is None:
            raise ValueError("no Neo4j backend configured (run: graphify backend set <uri>)")
    from graphify.backends.neo4j import Neo4jBackend
    return Neo4jBackend(
        cfg["uri"],
        cfg.get("user", "neo4j"),
        _resolve_password(),
        database=cfg.get("database", "neo4j"),
        branch=branch if branch is not None else current_branch(),
    )


def current_branch(repo_root: "Path | str | None" = None) -> str:
    """Git branch used to tag graph data in the backend.

    ``GRAPHIFY_BRANCH`` env overrides (CI escape hatch). Detached HEAD becomes
    ``detached-<shortsha>`` (still queryable and prunable). Outside a repo, or
    with git missing/hanging, falls back to ``_default``.
    """
    env = os.environ.get("GRAPHIFY_BRANCH")
    if env:
        return env

    def _git(*args: str) -> str | None:
        try:
            r = subprocess.run(
                ["git", *args],
                cwd=str(repo_root) if repo_root else None,
                capture_output=True, text=True, timeout=3,
            )
        except (OSError, subprocess.TimeoutExpired):
            return None
        if r.returncode != 0:
            return None
        return r.stdout.strip() or None

    name = _git("rev-parse", "--abbrev-ref", "HEAD")
    if not name:
        return "_default"
    if name == "HEAD":  # detached
        sha = _git("rev-parse", "--short", "HEAD")
        return f"detached-{sha}" if sha else "_default"
    return name


def load_graph_data_any(ref_or_path: str) -> tuple[dict, "Path | None"]:
    """Load a graph.json-shaped dict from a file path or a backend ref.

    Returns ``(data, out_dir)`` — ``out_dir`` is the directory holding the
    local sidecars (labels/learning/report), or None for a raw backend URI
    with no associated project directory.
    """
    if is_backend_ref(ref_or_path):
        backend = open_backend(ref_or_path)
        try:
            data = backend.load_graph_data()
        finally:
            backend.close()
        if data is None:
            raise FileNotFoundError(
                f"no graph for branch {current_branch()!r} in Neo4j backend "
                f"{ref_or_path} — run a build first"
            )
        return data, None
    p = Path(ref_or_path)
    from graphify.security import check_graph_file_size_cap
    check_graph_file_size_cap(p)
    return json.loads(p.read_text(encoding="utf-8")), p.parent


def ensure_out_gitignore(out_dir: "Path | str") -> None:
    """Drop a self-ignoring ``.gitignore`` (containing ``*``) into the output
    dir. Idempotent: created only when absent, never overwritten."""
    out = Path(out_dir)
    gi = out / ".gitignore"
    if gi.exists():
        return
    out.mkdir(parents=True, exist_ok=True)
    gi.write_text(_GITIGNORE_CONTENT, encoding="utf-8")


def materialize_from_backend(out_dir: "Path | str", backend) -> dict | None:
    """Pull the branch's graph from the backend into ``<out_dir>/graph.json``.

    Returns the materialized dict, or None when the branch has never been
    written (first run / empty branch) — in that case any existing local
    graph.json is left untouched so a pre-backend build can seed the branch.
    """
    data = backend.load_graph_data()
    if data is None:
        return None
    out = Path(out_dir)
    ensure_out_gitignore(out)
    from graphify.paths import write_json_atomic
    write_json_atomic(out / "graph.json", data, indent=2)
    return data


def push_after_write(out_dir: "Path | str", backend, *, prior: dict | None) -> dict[str, int]:
    """Push the freshly written ``<out_dir>/graph.json`` to the backend.

    ``prior`` is the dict :func:`materialize_from_backend` returned (delta
    push), or None for an authoritative full replace.
    """
    out = Path(out_dir)
    data = json.loads((out / "graph.json").read_text(encoding="utf-8"))
    counts = backend.save_graph_data(data, prior=prior)
    save_backend_state(out, version=backend.get_version())
    return counts


def load_backend_state(out_dir: "Path | str") -> dict:
    try:
        raw = json.loads((Path(out_dir) / _STATE_NAME).read_text(encoding="utf-8"))
        return raw if isinstance(raw, dict) else {}
    except (OSError, ValueError):
        return {}


def save_backend_state(out_dir: "Path | str", *, version, dirty: bool = False) -> None:
    from graphify.paths import write_json_atomic
    state: dict = {"version": version}
    if dirty:
        state["dirty"] = True
    write_json_atomic(Path(out_dir) / _STATE_NAME, state)


class BackendSync:
    """Materialize-before / push-after bracket for build/update in backend mode.

    Usage (both steps are no-ops when no backend is configured)::

        sync = BackendSync(out_dir, repo_root)
        if not sync.prepare():   # fail closed: backend configured but unreachable
            return False
        ...existing file-based rebuild, reading/writing <out>/graph.json...
        sync.push(changed=...)   # delta push; failure is a loud warning, not an error
        sync.close()

    State tracking (``.graphify_backend_state.json``) records the last DB
    version this checkout synced to, plus a ``dirty`` flag set when a push
    failed — i.e. the local graph.json is AHEAD of the DB. That flag is what
    keeps a later ``prepare()`` from clobbering the newer local cache with the
    older DB state: instead it diffs against the true DB content so the next
    successful push carries everything the failed one missed.
    """

    def __init__(self, out_dir: "Path | str", repo_root: "Path | str | None" = None):
        self.out = Path(out_dir)
        self.repo_root = repo_root
        self.cfg = backend_config(self.out)
        self.backend = None
        self.prior: dict | None = None
        # True when the DB is known to lag the local cache (a previous push
        # failed): push even if the rebuild itself found no local change.
        self._force_push = False

    @property
    def enabled(self) -> bool:
        return self.cfg is not None

    def prepare(self) -> bool:
        """Open the backend and set ``self.prior`` to the DB's current graph,
        refreshing the local graph.json cache when it is behind. Returns False
        (after printing an error) when the backend is configured but unusable —
        the caller must abort rather than silently fall back to file mode."""
        if not self.enabled:
            return True
        import sys
        try:
            self.backend = open_backend(self.cfg, branch=current_branch(self.repo_root),
                                        out_dir=self.out)
            db_version = self.backend.get_version()
        except Exception as exc:
            print(
                f"error: Neo4j backend configured but unusable ({exc}); "
                f"fix connectivity/credentials or run 'graphify backend unset'.",
                file=sys.stderr,
            )
            self.close()
            return False
        if db_version is None:
            # Branch never written: nothing to materialize; the push after the
            # rebuild seeds it with a full replace.
            self.prior = None
            return True
        state = load_backend_state(self.out)
        graph_file = self.out / "graph.json"
        in_sync = state.get("version") == db_version and not state.get("dirty")
        if in_sync and graph_file.exists():
            # Local cache already mirrors the DB — diff against it without
            # pulling the whole graph over the wire.
            try:
                self.prior = json.loads(graph_file.read_text(encoding="utf-8"))
                return True
            except (OSError, ValueError):
                pass  # unreadable cache: fall through to a fresh materialize
        try:
            if state.get("dirty") and state.get("version") == db_version:
                # Local is AHEAD (a previous push failed). Keep the local file;
                # fetch the true DB state as the delta base so the next push
                # carries the backlog — even if this rebuild changes nothing.
                self.prior = self.backend.load_graph_data()
                self._force_push = True
                return True
            if state.get("dirty"):
                print(
                    "[graphify] warning: local graph has un-pushed changes AND the "
                    "Neo4j branch changed externally; the DB wins (last-write-wins) "
                    "and the local cache is refreshed from it.",
                    file=sys.stderr,
                )
            self.prior = materialize_from_backend(self.out, self.backend)
            return True
        except Exception as exc:
            print(
                f"error: could not read the graph from Neo4j ({exc}); "
                f"fix connectivity or run 'graphify backend unset'.",
                file=sys.stderr,
            )
            self.close()
            return False

    def push(self, *, changed: bool = True) -> None:
        """Push ``<out>/graph.json`` to the backend after a successful local
        write. ``changed=False`` (the rebuild found nothing to do) still seeds
        a never-written branch. A push failure keeps the local graph, prints a
        loud warning and marks the state dirty for the next run."""
        if self.backend is None:
            return
        import sys
        if not changed and self.prior is not None and not self._force_push:
            return  # local == DB already; nothing to push
        try:
            counts = push_after_write(self.out, self.backend, prior=self.prior)
            branch = getattr(self.backend, "branch", "?")
            if self.prior is None:
                print(f"[graphify] Neo4j: seeded branch {branch!r} with "
                      f"{counts['nodes']} nodes / {counts['edges']} edges.")
            else:
                print(f"[graphify] Neo4j: branch {branch!r} updated "
                      f"(+{counts['nodes_upserted']} nodes, +{counts['edges_upserted']} edges, "
                      f"-{counts['nodes_removed']} nodes, -{counts['edges_removed']} edges).")
        except Exception as exc:
            state = load_backend_state(self.out)
            try:
                save_backend_state(self.out, version=state.get("version"), dirty=True)
            except OSError:
                pass
            print(
                f"[graphify] WARNING: push to Neo4j failed ({exc}). The local "
                f"graph.json is up to date but the Neo4j branch is BEHIND; the "
                f"next build/update will re-push the missing changes.",
                file=sys.stderr,
            )

    def close(self) -> None:
        if self.backend is not None:
            try:
                self.backend.close()
            except Exception:
                pass
            self.backend = None
