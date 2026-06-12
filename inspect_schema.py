"""Print the /user/register and /user/login request schemas of a running backend.

Asks the server's own OpenAPI spec what fields each auth endpoint requires, so
the adapter can be matched exactly instead of guessed. Works for any FastAPI /
jac-cloud server.

Usage:
    python3 inspect_schema.py [base_url]      # default http://localhost:8080
"""

import json
import sys
import urllib.request

base = (sys.argv[1] if len(sys.argv) > 1 else "http://localhost:8080").rstrip("/")
spec = json.load(urllib.request.urlopen(base + "/openapi.json"))
schemas = spec.get("components", {}).get("schemas", {})


def field_type(s):
    if "type" in s:
        return s["type"]
    if "$ref" in s:
        return s["$ref"].split("/")[-1]
    if "anyOf" in s:
        return " | ".join(field_type(x) for x in s["anyOf"])
    return "?"


for path in ["/user/register", "/user/login"]:
    post = spec.get("paths", {}).get(path, {}).get("post")
    if not post:
        print("===", path, "=== (endpoint not found)")
        continue
    sch = (post.get("requestBody", {})
              .get("content", {})
              .get("application/json", {})
              .get("schema", {}))
    body = schemas.get(sch.get("$ref", "").split("/")[-1], sch)
    print("===", path, "===")
    print("  required:", body.get("required"))
    for name, s in body.get("properties", {}).items():
        print(f"    - {name}: {field_type(s)}")
