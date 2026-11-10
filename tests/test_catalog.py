"""Tests for the SQL-facing catalog shape and discovery metadata."""

from __future__ import annotations

import json
import re
from pathlib import Path

from vgi_nascar import functions as f
from vgi_nascar.worker import AGENT_TASKS, NASCAR_CATALOG

ROOT = Path(__file__).resolve().parent.parent


def test_catalog_surface() -> None:
    schema = NASCAR_CATALOG.schemas[0]
    assert NASCAR_CATALOG.name == "nascar"
    assert schema.path == ["main"]
    assert {function.Meta.name for function in schema.functions} == {
        "schedule",
        "all_current_schedule",
        "all_current_standings",
        "all_last_race_results",
        "all_live_race",
        "season_results",
        "race_results",
        "session_results",
        "stage_results",
        "race_cautions",
        "race_leaders",
        "lap_times",
        "pit_stops",
        "lap_notes",
        "loop_stats",
        "drivers",
    }
    assert {table.name: table.function for table in schema.tables} == {
        "current_standings": f.CurrentStandingsFunction,
        "current_schedule": f.CurrentScheduleFunction,
        "last_race_results": f.LastRaceResultsFunction,
        "live_race": f.LiveRaceFunction,
    }


def _key_of(table, kind: str) -> list[tuple[str, ...]]:
    return [
        tuple(name for path in record.definition.column_paths for name in path)
        for record in table.constraints
        if record.definition.kind == kind
    ]


def test_table_keys() -> None:
    tables = {table.name: table for table in NASCAR_CATALOG.schemas[0].tables}
    assert _key_of(tables["current_standings"], "PRIMARY_KEY") == [("series_id", "driver_id")]
    assert _key_of(tables["current_schedule"], "PRIMARY_KEY") == [("race_id",)]
    assert _key_of(tables["last_race_results"], "PRIMARY_KEY") == [("race_id", "driver_id")]
    assert _key_of(tables["live_race"], "NOT_NULL") == [("race_id",)]


def test_key_columns_are_non_nullable_in_the_schema() -> None:
    for table in NASCAR_CATALOG.schemas[0].tables:
        schema = table.function.FIXED_SCHEMA
        for (column,) in _key_of(table, "NOT_NULL"):
            assert not schema.field(column).nullable, (table.name, column)


def test_tables_carry_titles_and_examples() -> None:
    for table in NASCAR_CATALOG.schemas[0].tables:
        for tag in (
            "vgi.title",
            "vgi.subtitle",
            "vgi.doc_llm",
            "vgi.doc_md",
            "vgi.keywords",
            "vgi.example_queries",
        ):
            assert table.tags.get(tag), (table.name, tag)
        assert set(table.column_comments) == set(table.function.FIXED_SCHEMA.names)


def test_race_functions_take_race_id_positionally() -> None:
    metadata = f.RaceResultsFunction.get_metadata()
    parameters = {parameter.name: parameter for parameter in metadata.parameters}
    assert set(parameters) == {"race_id", "season"}
    assert parameters["race_id"].required and not parameters["season"].required


def test_function_examples_reference_real_functions() -> None:
    names = {function.Meta.name for function in f.FUNCTIONS} | {
        t.name for t in NASCAR_CATALOG.schemas[0].tables
    }
    for function in f.FUNCTIONS:
        for example in json.loads(function.Meta.tags["vgi.example_queries"]):
            referenced = set(re.findall(r"nascar\.main\.(\w+)", example["sql"]))
            assert referenced and referenced <= names, (function.Meta.name, referenced)


def test_catalog_discovery_contract_is_public_and_machine_readable() -> None:
    examples = json.loads(NASCAR_CATALOG.tags["vgi.executable_examples"])
    tasks = json.loads(NASCAR_CATALOG.tags["vgi.agent_test_tasks"])
    assert all({"name", "description", "sql", "expected_result"} <= example.keys() for example in examples)
    assert 6 <= len(tasks) <= 10
    assert all(set(task) == {"name", "prompt"} for task in tasks)
    assert len({task["name"] for task in tasks}) == len(tasks)


def test_agent_test_file_matches_catalog_tasks() -> None:
    text = (ROOT / "vgi-agent-tests.yaml").read_text()
    names = re.findall(r"^  - name: (\S+)$", text, re.MULTILINE)
    assert names == [task["name"] for task in AGENT_TASKS]
    # Every task has an executable oracle. Tasks over moving current_* data must use
    # check_sql: an exact reference compare cannot pass once the data changes.
    blocks = re.split(r"^  - name: ", text, flags=re.MULTILINE)[1:]
    for block in blocks:
        assert ("check_sql:" in block) != ("reference_sql:" in block), block.splitlines()[0]
        if re.search(r"current_|last_race_results|live_race", block):
            assert "check_sql:" in block, block.splitlines()[0]
