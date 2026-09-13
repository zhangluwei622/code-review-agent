"""A deliberately closed JSON Schema subset; unsupported declarations fail registration.

No coercion, remote references, regex engines or permissive parsing. The same
validator applies to model arguments and each streamed output record.
"""

from review_agent.contracts import AgentError

KEYS = {
    "object": {"type", "properties", "required", "additionalProperties"},
    "array": {"type", "items", "minItems", "maxItems"},
    "string": {"type", "minLength", "maxLength", "enum"},
    "integer": {"type", "minimum", "maximum", "enum"},
    "boolean": {"type"},
    "null": {"type"},
}


def check_schema(schema, depth=0):
    def require(condition):
        if not condition:
            raise AgentError("INVALID_TOOL_SCHEMA")

    require(isinstance(schema, dict) and depth <= 12)
    kind = schema.get("type")
    require(isinstance(kind, str) and kind in KEYS)
    require(not (schema.keys() - KEYS[kind]))
    if kind == "object":
        require(schema.get("additionalProperties") is False)
        props, required = schema.get("properties"), schema.get("required")
        require(isinstance(props, dict) and len(props) <= 50)
        require(isinstance(required, list) and all(type(k) is str for k in required))
        require(len(set(required)) == len(required) and set(required) <= props.keys())
        for child in props.values():
            check_schema(child, depth + 1)
    elif kind == "array":
        check_schema(schema.get("items"), depth + 1)
        require(type(schema.get("maxItems")) is int and 0 <= schema["maxItems"] <= 10000)
        require(type(schema.get("minItems", 0)) is int)
        require(0 <= schema.get("minItems", 0) <= schema["maxItems"])
    elif kind == "string":
        require(type(schema.get("maxLength")) is int and 0 <= schema["maxLength"] <= 65536)
        require(type(schema.get("minLength", 0)) is int)
        require(0 <= schema.get("minLength", 0) <= schema["maxLength"])
    elif kind == "integer":
        require(type(schema.get("minimum")) is int and type(schema.get("maximum")) is int)
        require(schema["minimum"] <= schema["maximum"])
    if "enum" in schema:
        require(isinstance(schema["enum"], list) and 0 < len(schema["enum"]) <= 100)
        for value in schema["enum"]:
            validate({k: v for k, v in schema.items() if k != "enum"}, value)


def validate(schema, value):
    kind = schema["type"]
    valid = {
        "object": type(value) is dict,
        "array": type(value) is list,
        "string": type(value) is str,
        "integer": type(value) is int,
        "boolean": type(value) is bool,
        "null": value is None,
    }[kind]
    if not valid or ("enum" in schema and value not in schema["enum"]):
        raise AgentError("TOOL_SCHEMA_REJECTED")
    if kind == "object":
        if value.keys() - schema["properties"].keys() or set(schema["required"]) - value.keys():
            raise AgentError("TOOL_SCHEMA_REJECTED")
        for key, child in value.items():
            validate(schema["properties"][key], child)
    elif kind == "array":
        if not schema.get("minItems", 0) <= len(value) <= schema["maxItems"]:
            raise AgentError("TOOL_SCHEMA_REJECTED")
        for child in value:
            validate(schema["items"], child)
    elif kind == "string":
        if not schema.get("minLength", 0) <= len(value) <= schema["maxLength"]:
            raise AgentError("TOOL_SCHEMA_REJECTED")
    elif kind == "integer" and not schema["minimum"] <= value <= schema["maximum"]:
        raise AgentError("TOOL_SCHEMA_REJECTED")
