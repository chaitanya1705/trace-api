"""Read-only schema introspection of a customer's Postgres (metadata only: no table rows are ever selected).

Guards: the session is read-only (`default_transaction_read_only`), has a statement timeout, and every query reads
information_schema / pg_catalog. Column defaults are redacted. `pg_class.reltuples` is the planner's row ESTIMATE (a statistic,
not data) and is labeled as such.
"""
from __future__ import annotations

from typing import Any

import psycopg
from psycopg.conninfo import conninfo_to_dict
from psycopg.rows import dict_row

from app.connectors.base import ProviderError, Stage, VerifyResult
from app.redact import redact_text

MAX_TABLES = 300
SYSTEM_SCHEMAS = ("pg_catalog", "information_schema", "pg_toast")


def parse_dsn(dsn: str) -> dict[str, Any]:
    try:
        d = conninfo_to_dict(dsn.strip())
    except psycopg.ProgrammingError:
        raise ProviderError("bad_connection_string", "That is not a valid Postgres connection string") from None
    if not d.get("host") and not d.get("hostaddr"):
        raise ProviderError("bad_connection_string", "The connection string has no host")
    return d


def public_config(d: dict[str, Any]) -> dict[str, Any]:
    """Non-secret parts only, for display."""
    return {"host": d.get("host") or d.get("hostaddr"), "port": str(d.get("port") or 5432), "database": d.get("dbname"), "user": d.get("user")}


async def _connect(dsn: str) -> psycopg.AsyncConnection:
    d = parse_dsn(dsn)
    try:
        return await psycopg.AsyncConnection.connect(
            dsn, connect_timeout=8, row_factory=dict_row, autocommit=False, prepare_threshold=None,
            options="-c default_transaction_read_only=on -c statement_timeout=15000 -c idle_in_transaction_session_timeout=20000",
            application_name="trace-schema-reader",
        )
    except psycopg.OperationalError as e:
        msg = str(e).splitlines()[0] if str(e) else "connection failed"
        if "password authentication failed" in msg or "no password supplied" in msg:
            raise ProviderError("bad_credentials", "Postgres rejected the user or password") from None
        if "does not exist" in msg:
            raise ProviderError("not_found", redact_text(msg)[:200]) from None
        raise ProviderError("provider_unavailable", f"Cannot connect to {d.get('host')}: {redact_text(msg)[:160]}", retryable=True) from None


async def verify(dsn: str) -> VerifyResult:
    st: list[Stage] = []
    try:
        cfg = public_config(parse_dsn(dsn))
        conn = await _connect(dsn)
    except ProviderError as e:
        return VerifyResult([Stage(e.code, "Connect", "fail", e.message)])
    try:
        v = (await (await conn.execute("SHOW server_version")).fetchone())["server_version"]
        st.append(Stage("connect", "Connect", "ok", f"Connected to {cfg['database']} on {cfg['host']} (PostgreSQL {v}).", "connect"))
        who = await (await conn.execute(
            "SELECT r.rolsuper, has_database_privilege(current_user, current_database(), 'CREATE') AS can_create FROM pg_roles r WHERE r.rolname = current_user")).fetchone()
        if who and (who["rolsuper"] or who["can_create"]):
            st.append(Stage("privileges", "Check what the role can do", "warn",
                            "This role can create objects" + (" and is a superuser" if who["rolsuper"] else "")
                            + ". TRACE only reads schema metadata in a read-only session; prefer a read-only role.", "pg_roles"))
        else:
            st.append(Stage("privileges", "Check what the role can do", "ok", "The role is read-only.", "pg_roles"))
        n = (await (await conn.execute(
            "SELECT count(*) AS n FROM pg_class c JOIN pg_namespace s ON s.oid = c.relnamespace WHERE c.relkind IN ('r','p') AND s.nspname <> ALL(%s) AND s.nspname NOT LIKE 'pg_temp%%'",
            (list(SYSTEM_SCHEMAS),))).fetchone())["n"]
        st.append(Stage("tables", "Read the schema", "ok" if n else "warn", f"{n} tables visible." if n else "No tables are visible to this role.", "pg_catalog"))
        st.append(Stage("rows", "Row data", "skip", "TRACE does not read table rows, only structure."))
        return VerifyResult(st, {"database": cfg["database"], "server_version": v}, cfg)
    except psycopg.Error as e:
        st.append(Stage("query", "Read the schema", "fail", redact_text(str(e).splitlines()[0])[:200]))
        return VerifyResult(st)
    finally:
        await conn.close()


async def introspect(dsn: str) -> dict[str, Any]:
    conn = await _connect(dsn)
    try:
        sysx = list(SYSTEM_SCHEMAS)
        tbls = await (await conn.execute(
            "SELECT c.oid, s.nspname AS schema, c.relname AS name, c.relkind AS kind, GREATEST(c.reltuples, 0)::bigint AS approx_rows"
            " FROM pg_class c JOIN pg_namespace s ON s.oid = c.relnamespace"
            " WHERE c.relkind IN ('r','p','v','m') AND s.nspname <> ALL(%s) AND s.nspname NOT LIKE 'pg_temp%%' AND NOT c.relispartition"
            " ORDER BY s.nspname, c.relname LIMIT %s", (sysx, MAX_TABLES + 1))).fetchall()
        truncated = len(tbls) > MAX_TABLES
        tbls = tbls[:MAX_TABLES]
        oids = [t["oid"] for t in tbls]
        cols = await (await conn.execute(
            "SELECT a.attrelid AS oid, a.attname AS name, a.attnum AS ord, format_type(a.atttypid, a.atttypmod) AS type, NOT a.attnotnull AS nullable,"
            " pg_get_expr(d.adbin, d.adrelid) AS default_expr FROM pg_attribute a LEFT JOIN pg_attrdef d ON d.adrelid = a.attrelid AND d.adnum = a.attnum"
            " WHERE a.attrelid = ANY(%s) AND a.attnum > 0 AND NOT a.attisdropped ORDER BY a.attrelid, a.attnum", (oids,))).fetchall()
        cons = await (await conn.execute(
            "SELECT c.conrelid AS oid, c.contype, c.conname, c.confrelid, c.conkey, c.confkey,"
            " (SELECT array_agg(a.attname ORDER BY k.i) FROM unnest(c.conkey) WITH ORDINALITY k(n,i) JOIN pg_attribute a ON a.attrelid = c.conrelid AND a.attnum = k.n) AS cols,"
            " (SELECT array_agg(a.attname ORDER BY k.i) FROM unnest(c.confkey) WITH ORDINALITY k(n,i) JOIN pg_attribute a ON a.attrelid = c.confrelid AND a.attnum = k.n) AS ref_cols,"
            " rs.nspname AS ref_schema, rc.relname AS ref_table"
            " FROM pg_constraint c LEFT JOIN pg_class rc ON rc.oid = c.confrelid LEFT JOIN pg_namespace rs ON rs.oid = rc.relnamespace"
            " WHERE c.conrelid = ANY(%s) AND c.contype IN ('p','f','u')", (oids,))).fetchall()
        idx = await (await conn.execute(
            "SELECT i.indrelid AS oid, ic.relname AS name, i.indisunique AS is_unique, i.indisprimary AS is_primary,"
            " (SELECT array_agg(a.attname ORDER BY k.i) FROM unnest(i.indkey::int[]) WITH ORDINALITY k(n,i) JOIN pg_attribute a ON a.attrelid = i.indrelid AND a.attnum = k.n WHERE k.n > 0) AS cols"
            " FROM pg_index i JOIN pg_class ic ON ic.oid = i.indexrelid WHERE i.indrelid = ANY(%s)", (oids,))).fetchall()
        meta = await (await conn.execute("SELECT current_database() AS db, current_setting('server_version') AS v")).fetchone()
        out = []
        for t in tbls:
            o = t["oid"]
            pk = next((c["cols"] for c in cons if c["oid"] == o and c["contype"] == "p"), []) or []
            out.append({
                "schema": t["schema"], "name": t["name"], "kind": {"r": "table", "p": "table", "v": "view", "m": "materialized_view"}[t["kind"]],
                "approx_rows": int(t["approx_rows"]),
                "columns": [{"name": c["name"], "type": c["type"], "nullable": c["nullable"], "primary_key": c["name"] in pk,
                             "default": redact_text(c["default_expr"])[:120] if c["default_expr"] else None} for c in cols if c["oid"] == o],
                "primary_key": pk,
                "foreign_keys": [{"name": c["conname"], "columns": c["cols"] or [], "ref_schema": c["ref_schema"], "ref_table": c["ref_table"], "ref_columns": c["ref_cols"] or []}
                                 for c in cons if c["oid"] == o and c["contype"] == "f"],
                "unique": [c["cols"] or [] for c in cons if c["oid"] == o and c["contype"] == "u"],
                "indexes": [{"name": i["name"], "columns": i["cols"] or [], "unique": i["is_unique"]} for i in idx if i["oid"] == o],
            })
        return {"database": meta["db"], "server_version": meta["v"], "tables": out, "truncated": truncated}
    finally:
        await conn.rollback()
        await conn.close()
