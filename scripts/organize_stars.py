#!/usr/bin/env python3
"""Daily organizer for GitHub starred repos into Star Lists.

- Fetches all starred repos and existing Star Lists.
- Skips repos already present in any list.
- Classifies new repos with GitHub Models (AI).
- Creates lists as needed and assigns repos.
"""
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

if not STARS_TOKEN:
    print("STARS_TOKEN is required", file=sys.stderr)
    sys.exit(1)

os.environ["GH_TOKEN"] = STARS_TOKEN


def gh(*args):
    return subprocess.run(["gh", *args], check=True, capture_output=True, text=True)


def gql(query, **variables):
    cmd = ["gh", "api", "graphql", "-f", f"query={query}"]
    for k, v in variables.items():
        if v is not None:
            cmd.extend(["-f", f"{k}={v}"])
    proc = gh(*cmd[1:]) if False else subprocess.run(cmd, check=True, capture_output=True, text=True)
    body = json.loads(proc.stdout)
    if body.get("errors"):
        raise RuntimeError(json.dumps(body["errors"]))
    return body["data"]


def fetch_starred():
    proc = subprocess.run(
        ["gh", "api", "/user/starred?per_page=100", "--paginate", "--jq", ".[]"],
        check=True, capture_output=True, text=True,
    )
    repos = []
    for line in proc.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        repos.append(json.loads(line))
    return repos


def fetch_lists():
    data = gql("query { viewer { lists(first: 100) { nodes { id name description items(first: 1) { totalCount } } } } }")
    lists = data["viewer"]["lists"]["nodes"]
    # fetch full membership ids per list
    result = []
    for ul in lists:
        members = []
        cursor = None
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
            d = gql(q, listId=ul["id"], cursor=cursor)
            conn = d["node"]["items"]
            members.extend(n for n in conn["nodes"] if n)
            if not conn["pageInfo"]["hasNextPage"]:
                break
            cursor = conn["pageInfo"]["endCursor"]
        result.append({"id": ul["id"], "name": ul["name"], "description": ul.get("description", ""), "members": members})
    return result


def create_list(name):
    mutation = "mutation($name: String!) { createUserList(input: {name: $name}) { list { id name } } }"
    d = gql(mutation, name=name)
    return d["createUserList"]["list"]


def assign_repo(node_id, list_id):
    mutation = f'mutation {{ updateUserListsForItem(input: {{itemId: "{node_id}", listIds: ["{list_id}"]}}) {{ clientMutationId }} }}'
    # use subprocess to allow retries outside
    for attempt in range(6):
        try:
            proc = subprocess.run(
                ["gh", "api", "graphql", "-f", f"query={mutation}"],
                capture_output=True, text=True, timeout=60,
            )
            if proc.returncode == 0:
                body = json.loads(proc.stdout)
                if body.get("errors"):
                    msg = json.dumps(body["errors"])
                    if "RATE_LIMIT" in msg or "rate limit" in msg.lower() or "secondary" in msg.lower():
                        time.sleep(attempt * 20 + 5)
                        continue
                    raise RuntimeError(msg)
                if body.get("data", {}).get("updateUserListsForItem") is not None:
                    return True
            else:
                out = proc.stdout + proc.stderr
                if "RATE_LIMIT" in out or "rate limit" in out.lower() or "secondary" in out.lower():
                    time.sleep(attempt * 20 + 5)
                    continue
                raise RuntimeError(out[:300])
        except Exception as e:
            time.sleep(attempt * 10 + 5)
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
        # strip fences
        lines = text.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].startswith("```"):
            lines = lines[:-1]
        text = "\n".join(lines).strip()
    # find first { ... }
    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end != -1:
        text = text[start:end + 1]
    return json.loads(text)


def classify_batch(repos, categories):
    # repos: list of dicts with id, full_name, description, language, topics
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
    # validate
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

    # classify in batches of 50
    batch_size = 50
    classification = {}
    for i in range(0, len(unorganized), batch_size):
        batch = unorganized[i:i + batch_size]
        print(f"Classifying batch {i // batch_size + 1} ({len(batch)} repos)...")
        mapping = classify_batch(batch, categories)
        classification.update(mapping)

    # ensure lists exist for all categories used
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

    # assign
    # map repo id -> node_id
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