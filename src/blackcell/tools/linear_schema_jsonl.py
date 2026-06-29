"""Stream Linear GraphQL introspection records as JSONL."""

import argparse
import json
import os
import sys
from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from hashlib import sha256
from typing import Any, Protocol, TextIO

from pydantic import SecretStr

from blackcell.adapters.linear_graphql import LINEAR_GRAPHQL_URL, LinearGraphQLTransport

LINEAR_APOLLO_SCHEMA_REFERENCE_URL = (
    "https://studio.apollographql.com/public/Linear-API/variant/current/schema/reference"
)

TYPE_REF = """
fragment TypeRef on __Type {
  kind
  name
  ofType {
    kind
    name
    ofType {
      kind
      name
      ofType {
        kind
        name
        ofType {
          kind
          name
          ofType {
            kind
            name
            ofType {
              kind
              name
            }
          }
        }
      }
    }
  }
}
"""

INPUT_VALUE = """
fragment InputValue on __InputValue {
  name
  description
  type { ...TypeRef }
  defaultValue
}
"""

FULL_TYPE = """
fragment FullType on __Type {
  kind
  name
  description
  fields(includeDeprecated: $includeDeprecated) {
    name
    description
    args { ...InputValue }
    type { ...TypeRef }
    isDeprecated
    deprecationReason
  }
  inputFields { ...InputValue }
  interfaces { ...TypeRef }
  enumValues(includeDeprecated: $includeDeprecated) {
    name
    description
    isDeprecated
    deprecationReason
  }
  possibleTypes { ...TypeRef }
}
"""

SCHEMA_INDEX_QUERY = (
    TYPE_REF
    + INPUT_VALUE
    + """
query SchemaIndex {
  __schema {
    queryType { name }
    mutationType { name }
    subscriptionType { name }
    directives {
      name
      description
      isRepeatable
      locations
      args { ...InputValue }
    }
    types {
      kind
      name
    }
  }
}
"""
)


class GraphQLExecutor(Protocol):
    def execute(
        self,
        query: str,
        variables: dict[str, Any] | None = None,
        *,
        mutation: bool = False,
    ) -> dict[str, Any]: ...


@dataclass(frozen=True, slots=True)
class SchemaStreamStats:
    records: int
    digest: str


def iter_chunks[T](values: Sequence[T], size: int) -> Iterator[Sequence[T]]:
    if size <= 0:
        raise ValueError("chunk size must be positive")
    for index in range(0, len(values), size):
        yield values[index : index + size]


def iter_schema_records(
    executor: GraphQLExecutor,
    *,
    endpoint: str = LINEAR_GRAPHQL_URL,
    reference_url: str = LINEAR_APOLLO_SCHEMA_REFERENCE_URL,
    batch_size: int = 20,
    include_deprecated: bool = True,
) -> Iterator[dict[str, Any]]:
    yield {
        "record": "schema_source",
        "provider": "linear",
        "graph": "Linear-API",
        "variant": "current",
        "endpoint": endpoint,
        "reference_url": reference_url,
        "introspection": True,
    }
    schema = executor.execute(SCHEMA_INDEX_QUERY)["__schema"]
    type_index = sorted(
        (
            {"kind": item["kind"], "name": item["name"]}
            for item in schema["types"]
            if item.get("name")
        ),
        key=lambda item: (item["kind"], item["name"]),
    )
    yield {
        "record": "schema",
        "query_type": _type_name(schema.get("queryType")),
        "mutation_type": _type_name(schema.get("mutationType")),
        "subscription_type": _type_name(schema.get("subscriptionType")),
        "type_count": len(type_index),
        "directive_count": len(schema["directives"]),
    }
    for directive in sorted(schema["directives"], key=lambda item: item["name"]):
        yield {"record": "directive", "name": directive["name"], "directive": directive}

    for chunk in iter_chunks(type_index, batch_size):
        data = executor.execute(
            _type_batch_query(chunk),
            {"includeDeprecated": include_deprecated},
        )
        for index, type_ref in enumerate(chunk):
            type_data = data[f"t{index}"]
            yield {
                "record": "type",
                "name": type_ref["name"],
                "kind": type_ref["kind"],
                "type": type_data,
            }


def write_jsonl(records: Iterable[Mapping[str, Any]], stream: TextIO) -> SchemaStreamStats:
    digest = sha256()
    count = 0
    for record in records:
        encoded = _canonical(record)
        digest.update(encoded)
        digest.update(b"\n")
        stream.write(encoded.decode("utf-8") + "\n")
        count += 1
    checksum = digest.hexdigest()
    final = {
        "record": "schema_digest",
        "algorithm": "sha256",
        "digest": checksum,
        "records": count,
    }
    stream.write(_canonical(final).decode("utf-8") + "\n")
    return SchemaStreamStats(records=count, digest=checksum)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Stream Linear GraphQL introspection records as JSONL.",
    )
    parser.add_argument("--endpoint", default=LINEAR_GRAPHQL_URL)
    parser.add_argument("--reference-url", default=LINEAR_APOLLO_SCHEMA_REFERENCE_URL)
    parser.add_argument("--batch-size", type=int, default=20)
    parser.add_argument(
        "--exclude-deprecated",
        action="store_true",
        help="Exclude deprecated fields and enum values from introspection records.",
    )
    args = parser.parse_args(argv)

    api_key = os.environ.get("LINEAR_API_KEY")
    if not api_key:
        sys.stderr.write("LINEAR_API_KEY is required for Linear schema introspection.\n")
        return 2
    with LinearGraphQLTransport(SecretStr(api_key), endpoint=args.endpoint) as transport:
        write_jsonl(
            iter_schema_records(
                transport,
                endpoint=args.endpoint,
                reference_url=args.reference_url,
                batch_size=args.batch_size,
                include_deprecated=not args.exclude_deprecated,
            ),
            sys.stdout,
        )
    return 0


def _type_batch_query(types: Sequence[Mapping[str, str]]) -> str:
    fields = "\n".join(
        f"  t{index}: __type(name: {json.dumps(item['name'])}) {{ ...FullType }}"
        for index, item in enumerate(types)
    )
    return (
        TYPE_REF
        + INPUT_VALUE
        + FULL_TYPE
        + f"""
query TypeBatch($includeDeprecated: Boolean!) {{
{fields}
}}
"""
    )


def _canonical(value: Mapping[str, Any]) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _type_name(value: Mapping[str, Any] | None) -> str | None:
    return str(value["name"]) if value and value.get("name") else None


if __name__ == "__main__":
    raise SystemExit(main())
