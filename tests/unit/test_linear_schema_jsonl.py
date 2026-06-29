"""Linear schema JSONL streaming utility."""

import json
from io import StringIO
from typing import Any

from blackcell.tools.linear_schema_jsonl import iter_schema_records, write_jsonl


class FakeSchemaExecutor:
    def __init__(self) -> None:
        self.queries: list[tuple[str, dict[str, Any] | None]] = []

    def execute(
        self,
        query: str,
        variables: dict[str, Any] | None = None,
        *,
        mutation: bool = False,
    ) -> dict[str, Any]:
        assert mutation is False
        self.queries.append((query, variables))
        if "__schema" in query:
            return {
                "__schema": {
                    "queryType": {"name": "Query"},
                    "mutationType": {"name": "Mutation"},
                    "subscriptionType": None,
                    "directives": [
                        {
                            "name": "deprecated",
                            "description": "Marks deprecated schema members.",
                            "isRepeatable": False,
                            "locations": ["FIELD_DEFINITION", "ENUM_VALUE"],
                            "args": [],
                        }
                    ],
                    "types": [
                        {"kind": "OBJECT", "name": "Query"},
                        {"kind": "INPUT_OBJECT", "name": "ProjectCreateInput"},
                    ],
                }
            }
        assert variables == {"includeDeprecated": True}
        if "t1:" in query:
            return {
                "t0": _type("ProjectCreateInput", "INPUT_OBJECT"),
                "t1": _type("Query", "OBJECT"),
            }
        return {"t0": _type("ProjectCreateInput", "INPUT_OBJECT")}


def test_iter_schema_records_streams_index_directives_and_batched_types() -> None:
    executor = FakeSchemaExecutor()

    records = list(iter_schema_records(executor, batch_size=2))

    assert [record["record"] for record in records] == [
        "schema_source",
        "schema",
        "directive",
        "type",
        "type",
    ]
    assert records[0]["reference_url"].startswith("https://studio.apollographql.com/")
    assert records[1]["type_count"] == 2
    assert records[2]["name"] == "deprecated"
    assert [record["name"] for record in records[3:]] == ["ProjectCreateInput", "Query"]
    assert len(executor.queries) == 2


def test_iter_schema_records_uses_batch_pages() -> None:
    executor = FakeSchemaExecutor()

    list(iter_schema_records(executor, batch_size=1))

    assert len(executor.queries) == 3
    assert "t1:" not in executor.queries[1][0]
    assert "t1:" not in executor.queries[2][0]


def test_write_jsonl_appends_deterministic_digest_record() -> None:
    stream = StringIO()
    stats = write_jsonl(
        [
            {"record": "schema", "type_count": 1},
            {"record": "type", "name": "Query"},
        ],
        stream,
    )

    lines = [json.loads(line) for line in stream.getvalue().splitlines()]

    assert [line["record"] for line in lines] == ["schema", "type", "schema_digest"]
    assert lines[-1]["records"] == 2
    assert lines[-1]["digest"] == stats.digest
    assert stats.records == 2


def _type(name: str, kind: str) -> dict[str, Any]:
    return {
        "kind": kind,
        "name": name,
        "description": None,
        "fields": [],
        "inputFields": [],
        "interfaces": [],
        "enumValues": None,
        "possibleTypes": None,
    }
