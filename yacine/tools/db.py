"""Database tools (Phase C).

Supports SQLite (stdlib, always available), and Postgres/MySQL/Mongo when the
optional drivers are installed. Provides schema discovery, query execution, and
migration generation. All operations are sandbox-friendly: SQLite files live in
the workspace; remote DBs connect via connection strings passed by the agent.

Tools:
  db_query      -> run a SQL/aggregation query
  db_schema    -> discover tables/columns (SQLite + relational)
  db_migrate   -> generate an UP/DOWN migration SQL from a desired schema diff
"""
from __future__ import annotations

import json
import re
import sqlite3
from typing import Any, Dict

from .base import Tool, ToolResult
from .sandbox import Sandbox


def _conn_for(kind: str, target: str):
    """Return a live connection or raise a clear error. kind in
    sqlite|postgres|mysql|mongo."""
    if kind == "sqlite":
        path = target or ":memory:"
        return sqlite3.connect(path)
    if kind == "postgres":
        try:
            import psycopg2  # type: ignore
        except ImportError:
            raise RuntimeError("psycopg2 not installed (pip install psycopg2-binary)")
        return psycopg2.connect(target)
    if kind == "mysql":
        try:
            import pymysql  # type: ignore
        except ImportError:
            raise RuntimeError("pymysql not installed (pip install pymysql)")
        # Expect "user:pass@host:port/db"
        m = re.match(r"(.+):(.+)@(.+):(\d+)/(.+)", target)
        if not m:
            raise RuntimeError("mysql target must be user:pass@host:port/db")
        return pymysql.connect(host=m.group(3), port=int(m.group(4)),
                               user=m.group(1), password=m.group(2),
                               database=m.group(5))
    if kind == "mongo":
        try:
            from pymongo import MongoClient  # type: ignore
        except ImportError:
            raise RuntimeError("pymongo not installed (pip install pymongo)")
        return MongoClient(target)
    raise RuntimeError(f"Unsupported db kind: {kind}")


class DBQueryTool(Tool):
    name = "db_query"
    description = ("Execute a query against a database. For sqlite, target is a file "
                  "path (relative to workspace) or ':memory:'. For postgres/mysql, "
                  "target is a connection string. Returns rows (SQL) or docs (mongo).")
    parameters = {
        "type": "object",
        "properties": {
            "kind": {"type": "string", "enum": ["sqlite", "postgres", "mysql", "mongo"]},
            "target": {"type": "string", "description": "DB path or connection string."},
            "query": {"type": "string", "description": "SQL statement or mongo collection.command."},
            "limit": {"type": "integer", "description": "Max rows to return (default 50)."},
        },
        "required": ["kind", "query"],
    }

    def __init__(self, sandbox: Sandbox) -> None:
        self.sandbox = sandbox

    def run(self, kind: str, query: str, target: str = ":memory:", limit: int = 50) -> ToolResult:
        try:
            conn = _conn_for(kind, target if kind != "sqlite" else
                             str(self.sandbox.workspace / target) if target != ":memory:" else ":memory:")
        except Exception as e:
            return ToolResult(ok=False, output=f"Connection error: {e}")
        try:
            if kind == "mongo":
                client = conn
                db_name, coll, op = query.split("|", 2) if "|" in query else (query, "", "")
                data = list(getattr(client[db_name], coll).find())[:limit] if coll else []
                return ToolResult(ok=True, output=json.dumps(data, default=str, indent=2)[:4000])
            cur = conn.cursor()
            cur.execute(query)
            if query.strip().lower().startswith(("select", "pragma", "show", "with")):
                rows = cur.fetchmany(limit)
                cols = [d[0] for d in cur.description] if cur.description else []
                out = [dict(zip(cols, r)) for r in rows]
                return ToolResult(ok=True, output=json.dumps(out, default=str)[:4000],
                                  data={"columns": cols, "rows": len(out)})
            else:
                conn.commit()
                return ToolResult(ok=True, output=f"OK. rows affected: {cur.rowcount}")
        except Exception as e:
            return ToolResult(ok=False, output=f"Query error: {e}")
        finally:
            try:
                conn.close()
            except Exception:
                pass


class DBSchemaTool(Tool):
    name = "db_schema"
    description = "Discover the schema (tables and columns) of a database."
    parameters = {
        "type": "object",
        "properties": {
            "kind": {"type": "string", "enum": ["sqlite", "postgres", "mysql", "mongo"]},
            "target": {"type": "string"},
        },
        "required": ["kind", "target"],
    }

    def __init__(self, sandbox: Sandbox) -> None:
        self.sandbox = sandbox

    def run(self, kind: str, target: str = ":memory:") -> ToolResult:
        try:
            if kind == "sqlite":
                path = ":memory:" if target == ":memory:" else str(self.sandbox.workspace / target)
                conn = sqlite3.connect(path)
                cur = conn.cursor()
                cur.execute("SELECT name, type FROM sqlite_master WHERE type IN ('table','view')")
                tables = cur.fetchall()
                schema: Dict[str, Any] = {}
                for name, _ in tables:
                    cur.execute(f"PRAGMA table_info('{name}')")
                    cols = [{"name": c[1], "type": c[2]} for c in cur.fetchall()]
                    schema[name] = cols
                conn.close()
                return ToolResult(ok=True, output=json.dumps(schema, indent=2)[:4000], data=schema)
            # Relational via information_schema
            conn = _conn_for(kind, target)
            cur = conn.cursor()
            cur.execute("SELECT table_name FROM information_schema.tables WHERE table_schema='public'")
            tables = [r[0] for r in cur.fetchall()]
            schema = {}
            for t in tables:
                cur.execute(f"SELECT column_name, data_type FROM information_schema.columns WHERE table_name='{t}'")
                schema[t] = [{"name": c[0], "type": c[1]} for c in cur.fetchall()]
            conn.close()
            return ToolResult(ok=True, output=json.dumps(schema, indent=2)[:4000], data=schema)
        except Exception as e:
            return ToolResult(ok=False, output=f"Schema discovery error: {e}")


class DBMigrateTool(Tool):
    name = "db_migrate"
    description = ("Generate an UP/DOWN SQL migration from a desired schema description. "
                  "Provide a list of tables with columns; the tool emits CREATE TABLE (up) "
                  "and DROP TABLE (down) statements for SQLite-compatible SQL.")
    parameters = {
        "type": "object",
        "properties": {
            "name": {"type": "string", "description": "Migration name."},
            "schema": {"type": "string",
                       "description": "YAML/JSON: {table: {col: type, ...}}. e.g. 'users: {id: INTEGER PRIMARY KEY, name: TEXT}'"},
        },
        "required": ["name", "schema"],
    }

    def __init__(self, sandbox: Sandbox = None) -> None:
        self.sandbox = sandbox

    def run(self, name: str, schema: str) -> ToolResult:
        try:
            # Accept JSON or simple YAML-ish.
            try:
                tables = json.loads(schema)
            except json.JSONDecodeError:
                import yaml
                tables = yaml.safe_load(schema)
            if not isinstance(tables, dict):
                return ToolResult(ok=False, output="schema must be a mapping of table->columns")
        except Exception as e:
            return ToolResult(ok=False, output=f"Schema parse error: {e}")

        up, down = [], []
        for table, cols in tables.items():
            col_defs = ", ".join(f"{c} {t}" for c, t in cols.items())
            up.append(f"CREATE TABLE IF NOT EXISTS {table} ({col_defs});")
            down.append(f"DROP TABLE IF EXISTS {table};")
        migration = f"-- migration: {name}\n-- UP\n" + "\n".join(up) + \
                    "\n\n-- DOWN\n" + "\n".join(down) + "\n"
        return ToolResult(ok=True, output=migration, data={"name": name})
