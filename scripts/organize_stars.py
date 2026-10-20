#!/usr/bin/env python3
"""Daily organizer for GitHub starred repos into Star Lists."""
import json
import os
import subprocess
import sys
import time
import urllib.request
import urllib.error

STARS_TOKEN = os.environ.get("STARS_TOKEN") or os.environ.get("GH_TOKEN")
MODELS_TOKEN = os.environ.get("MODELS_TOKEN") or STARS_TOKEN
MODEL = os.environ.get("MODEL", "openai/gpt-4o-mini")
MODELS_ENDPOINT = os.environ.get("MODELS_ENDPOINT", "https://models.github.ai/inference/chat/completions")
GRAPHQL_ENDPOINT = "https://api.github.com/graphql"
REST_BASE = "https://api.github.com"

if not STARS_TOKEN:
    print("STARS_TOKEN is required", file=sys.stderr)
    sys.exit(1)


def graphql(query, variables=None):
    payload = {"query": query}
    if variables:
        payload["variables"] = variables
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        GRAPHQL_ENDPOINT,
        data=data,
        headers={
            "Authorization": f"Bearer {STARS_TOKEN}",
            "Content-Type": "application/json",
            "Accept": "application/vnd.github+json",
        },
        method="POST",
    )
    for attempt in range(6):
        try:
            with urllib.request.urlopen(req, timeout=120) as resp:
                body = json.loads(resp.read().decode("utf-8"))
            if body.get("errors"):
                msg = json.dumps(body["errors"])
                if "RATE_LIMIT" in msg or "rate limit" in msg.lower() or "secondary" in msg.lower():
                    time.sleep(attempt * 20 + 5)
                    req = urllib.request.Request(GRAPHQL_ENDPOINT, data=data, headers={"Authorization": f"Bearer {STARS_TOKEN}", "Content-Type": "application/json", "Accept": "application/vnd.github+json"}, method="POST")
                    continue
                # Partial errors (e.g. org-forbidden fine-grained access): proceed if data present
                if body.get("data") is not None:
                    print(f"GraphQL partial errors: {msg}", file=sys.stderr)
                    return body["data"]
                raise RuntimeError(msg)
            return body["data"]
        except urllib.error.HTTPError as e:
            body = e.read().decode()[:500]
            print(f"HTTP {e.code}: {body}", file=sys.stderr)
            if e.code == 429 or e.code >= 500:
                time.sleep(attempt * 20 + 5)
                continue
            raise RuntimeError(f"HTTP {e.code}: {body}")
        except Exception as e:
            print(f"EXC attempt {attempt}: {repr(e)}", file=sys.stderr)
            time.sleep(attempt * 10 + 5)
    raise RuntimeError("graphql retries exhausted")


def rest_get(path):
    url = REST_BASE + path
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {STARS_TOKEN}", "Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"})
    with urllib.request.urlopen(req, timeout=120) as resp:
        return json.loads(resp.read().decode("utf-8")), resp.headers


def fetch_starred():
    repos = []
    page = 1
    while True:
        data, headers = rest_get(f"/user/starred?per_page=100&page={page}")
        if not data:
            break
        repos.extend(data)
        # check link header for next
        link = headers.get("Link", "")
        if 'rel="next"' not in link:
            break
        page += 1
    return repos


def fetch_lists():
    data = graphql("query { viewer { lists(first: 100) { nodes { id name description } } } }")
    lists = data["viewer"]["lists"]["nodes"]
    result = []
    for ul in lists:
        members = []
        cursor = None
        print(f"  fetching list {ul['name']}...", file=sys.stderr)
        while True:
            q = """query($listId: ID!, $cursor: String) {
              node(id: $listId) {
                ... on UserList {
                  items(first: 100, after: $cursor) {
                    nodes { ... on Repository { id nameWithOwner } }
                    pageInfo { hasNextPage endCursor }
                  }
                }
              }
            }"""
            d = graphql(q, {"listId": ul["id"], "cursor": cursor})
            conn = d["node"]["items"]
            members.extend(n for n in (conn.get("nodes") or []) if n)
            if not conn["pageInfo"]["hasNextPage"]:
                break
            cursor = conn["pageInfo"]["endCursor"]
            print(f"    page done, next cursor={cursor}", file=sys.stderr)
        result.append({"id": ul["id"], "name": ul["name"], "description": ul.get("description", ""), "members": members})
    return result


def create_list(name):
    mutation = "mutation($name: String!) { createUserList(input: {name: $name}) { list { id name } } }"
    d = graphql(mutation, {"name": name})
    return d["createUserList"]["list"]


def assign_repo(node_id, list_id):
    mutation = "mutation($itemId: ID!, $listIds: [ID!]!) { updateUserListsForItem(input: {itemId: $itemId, listIds: $listIds}) { clientMutationId } }"
    for attempt in range(6):
        try:
            d = graphql(mutation, {"itemId": node_id, "listIds": [list_id]})
            if d.get("updateUserListsForItem") is not None:
                return True
            raise RuntimeError("null result")
        except Exception as e:
            msg = str(e)
            if "RATE_LIMIT" in msg or "rate limit" in msg.lower() or "secondary" in msg.lower():
                time.sleep(attempt * 20 + 5)
                continue
            raise
    raise RuntimeError(f"retries exhausted for {node_id}")


def call_model(messages):
    payload = {"model": MODEL, "messages": messages, "temperature": 0.2}
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        MODELS_ENDPOINT,
        data=data,
        headers={
            "Authorization": f"Bearer {MODELS_TOKEN}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    for attempt in range(5):
        try:
            with urllib.request.urlopen(req, timeout=120) as resp:
                body = json.loads(resp.read().decode("utf-8"))
            return body["choices"][0]["message"]["content"]
        except urllib.error.HTTPError as e:
            if e.code == 429:
                time.sleep(2 ** attempt * 5)
                continue
            raise
        except Exception:
            time.sleep(2 ** attempt * 5)
    raise RuntimeError("model call failed")


def parse_json(text):
    text = text.strip()
    if text.startswith("```"):
        lines = text.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].startswith("```"):
            lines = lines[:-1]
        text = "\n".join(lines).strip()
    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end != -1:
        text = text[start:end + 1]
    return json.loads(text)


def classify_batch(repos, categories):
    cat_list = ", ".join(f'"{c}"' for c in categories)
    items = []
    for r in repos:
        items.append({
            "id": str(r["id"]),
            "name": r["full_name"],
            "description": (r.get("description") or "")[:200],
            "language": r.get("language") or "",
            "topics": r.get("topics") or [],
        })
    prompt = (
        "You are classifying GitHub repositories into categories.\n"
        f"Allowed categories (choose exactly one per repo): [{cat_list}]\n"
        "Return a JSON object mapping repo id (string) to category name. "
        "Use only the allowed names. If unsure use \"Miscellaneous\".\n"
        "Repositories:\n" + json.dumps(items, ensure_ascii=False)
    )
    messages = [
        {"role": "system", "content": "You output only valid JSON, no markdown."},
        {"role": "user", "content": prompt},
    ]
    content = call_model(messages)
    mapping = parse_json(content)
    result = {}
    for r in repos:
        rid = str(r["id"])
        cat = mapping.get(rid, "Miscellaneous")
        if cat not in categories:
            cat = "Miscellaneous"
        result[rid] = cat
    return result


def main():
    print("Fetching starred repos...")
    starred = fetch_starred()
    print(f"  {len(starred)} starred repos")

    print("Fetching existing lists...")
    lists = fetch_lists()
    list_by_name = {l["name"]: l for l in lists}
    organized_ids = set()
    for l in lists:
        for m in l["members"]:
            organized_ids.add(m["id"])
    print(f"  {len(lists)} lists, {len(organized_ids)} organized repos")

    unorganized = [r for r in starred if r["node_id"] not in organized_ids]
    print(f"  {len(unorganized)} new/unorganized repos")
    if not unorganized:
        print("Nothing to do.")
        return

    categories = list(list_by_name.keys())
    if "Miscellaneous" not in categories:
        categories.append("Miscellaneous")

    batch_size = 50
    classification = {}
    for i in range(0, len(unorganized), batch_size):
        batch = unorganized[i:i + batch_size]
        print(f"Classifying batch {i // batch_size + 1} ({len(batch)} repos)...")
        mapping = classify_batch(batch, categories)
        classification.update(mapping)

    for cat in set(classification.values()):
        if cat not in list_by_name:
            if len(list_by_name) >= 32:
                print(f"  list cap reached, remapping {cat} -> Miscellaneous")
                for rid, c in list(classification.items()):
                    if c == cat:
                        classification[rid] = "Miscellaneous"
                continue
            print(f"  Creating new list: {cat}")
            new_list = create_list(cat)
            list_by_name[cat] = {"id": new_list["id"], "name": new_list["name"], "members": []}

    by_id = {str(r["id"]): r for r in starred}
    ok = 0
    fail = 0
    for rid, cat in classification.items():
        node_id = by_id[rid]["node_id"]
        list_id = list_by_name[cat]["id"]
        try:
            assign_repo(node_id, list_id)
            ok += 1
        except Exception as e:
            fail += 1
            print(f"  FAIL {by_id[rid]['full_name']}: {e}", file=sys.stderr)
        if (ok + fail) % 50 == 0:
            print(f"  progress {ok + fail}/{len(classification)} (ok={ok} fail={fail})")

    print(f"Done. assigned={ok} failed={fail}")


if __name__ == "__main__":
    main()