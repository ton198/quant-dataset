"""DuckDB-backed validation for bounded, read-only SQL queries."""

from __future__ import annotations

from typing import Any


class ReadonlySQLError(ValueError):
    """Raised when a query cannot be proven to be one safe SELECT statement."""


def validate_select(duckdb: Any, sql: str) -> None:
    """Require exactly one SELECT statement, rejecting DuckDB's PRAGMA rewrite."""
    statement_type = getattr(duckdb, "StatementType", None)
    select_type = getattr(statement_type, "SELECT", None)
    extract_statements = getattr(duckdb, "extract_statements", None)
    if select_type is None or not callable(extract_statements):
        raise ReadonlySQLError(
            "This DuckDB build lacks the SQL statement parser API required for safe "
            "query validation; refusing to execute the query"
        )
    try:
        statements = extract_statements(sql)
    except Exception as exc:
        raise ReadonlySQLError(f"Invalid SQL: {exc}") from exc
    if not isinstance(statements, list):
        raise ReadonlySQLError(
            "This DuckDB build returned an unsupported parser result; refusing to execute the query"
        )
    if len(statements) != 1:
        raise ReadonlySQLError("Exactly one SQL statement is required")
    try:
        parsed_type = statements[0].type
    except Exception as exc:
        raise ReadonlySQLError(
            "This DuckDB build lacks parsed statement classifications; "
            "refusing to execute the query"
        ) from exc
    if parsed_type != select_type:
        raise ReadonlySQLError(
            "Only a single SELECT statement (including SELECT CTEs) is allowed"
        )
    # DuckDB parses PRAGMA syntax into an equivalent SELECT over a pragma_*
    # table function, so its StatementType alone reports SELECT for that command.
    # Inspect only the parser-normalized query here; regular SQL is authorized by
    # the StatementType enum above, not by keyword-prefix matching.
    normalized_query = getattr(statements[0], "query", None)
    if not isinstance(normalized_query, str):
        raise ReadonlySQLError("DuckDB did not expose the parsed query; refusing to execute it")
    if normalized_query.lstrip().casefold().startswith("select * from pragma_"):
        raise ReadonlySQLError("PRAGMA statements are not allowed")
