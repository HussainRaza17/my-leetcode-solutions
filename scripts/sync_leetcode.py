import os, pathlib, time, random, json, re
import requests
from typing import Optional, Dict, Any

USERNAME = "mhrnaqvi17"
CSRFTOKEN = os.environ["LEETCODE_CSRF_TOKEN"]
SESSION = os.environ["LEETCODE_SESSION"]

API = "https://leetcode.com/graphql/"

LANG_EXT = {
    "python3": "py", "python": "py",
    "java": "java", "cpp": "cpp",
    "c": "c", "csharp":"cs", "javascript":"js", "typescript":"ts", "go":"go",
    "kotlin":"kt", "swift":"swift", "rust":"rs", "php":"php", "ruby":"rb",
    "mysql":"sql", "mssql":"sql", "oraclesql":"sql"
}

RECENT_QUERY = """
query recentAc($username: String!, $limit: Int!) {
  recentAcSubmissionList(username: $username, limit: $limit) {
    id
    lang
    timestamp
  }
}
"""

DETAILS_QUERY = """
query submissionDetails($id: Int!) {
  submissionDetails(submissionId: $id) {
    code
    lang
    question { questionId }
  }
}
"""

def make_session() -> requests.Session:
    s = requests.Session()
    s.headers.update({
        "Content-Type": "application/json",
        "Origin": "https://leetcode.com",
        "Referer": "https://leetcode.com/",
        "X-Requested-With": "XMLHttpRequest",
        "User-Agent": "Mozilla/5.0 (compatible; GitHubActionsBot/1.0)",
        "x-csrftoken": CSRFTOKEN,
        "Cookie": f"LEETCODE_SESSION={SESSION}; csrftoken={CSRFTOKEN};"
    })
    return s

def gql(session: requests.Session, query: str, variables: Dict[str, Any],
        operation_name: Optional[str] = None, referer: Optional[str] = None,
        retries: int = 3, backoff: float = 0.8) -> Dict[str, Any]:
    if referer:
        session.headers["Referer"] = referer
    payload = {"query": query, "variables": variables}
    if operation_name:
        payload["operationName"] = operation_name
    last_err = None
    for attempt in range(retries):
        r = session.post(API, json=payload, timeout=45)
        if r.status_code in (429, 500, 502, 503, 504):
            time.sleep(backoff * (attempt + 1))
            continue
        if r.status_code in (400, 403) and attempt < retries - 1:
            time.sleep(backoff * (attempt + 1) + random.random() * 0.4)
            last_err = r
            continue
        try:
            r.raise_for_status()
        except Exception as e:
            # surface minimal info
            msg = f"GraphQL HTTP {r.status_code} for op={operation_name}"
            raise RuntimeError(msg) from e
        data = r.json()
        if "errors" in data and attempt < retries - 1:
            time.sleep(backoff * (attempt + 1))
            last_err = data
            continue
        return data
    if isinstance(last_err, requests.Response):
        raise RuntimeError(f"GraphQL final HTTP {last_err.status_code} for op={operation_name}")
    raise RuntimeError("GraphQL failed")

def fetch_recent(session: requests.Session, username: str, limit: int = 20):
    data = gql(
        session,
        RECENT_QUERY,
        {"username": username, "limit": limit},
        operation_name="recentAc",
        referer=f"https://leetcode.com/{username}/"
    )
    return (data.get("data") or {}).get("recentAcSubmissionList") or []

def fetch_submission_via_graphql(session: requests.Session, sub_id: int):
    data = gql(
        session,
        DETAILS_QUERY,
        {"id": sub_id},
        operation_name="submissionDetails",
        referer=f"https://leetcode.com/submissions/detail/{sub_id}/"
    )
    return (data.get("data") or {}).get("submissionDetails")

def extract_from_next_data(html: str) -> Optional[Dict[str, Any]]:
    """
    Parse __NEXT_DATA__ JSON from the submission detail page and search for a
    dict containing keys: code, lang, question{questionId}.
    """
    # Find the __NEXT_DATA__ script
    m = re.search(r'<script id="__NEXT_DATA__" type="application/json">(.+?)</script>', html, re.S)
    if not m:
        return None
    try:
        j = json.loads(m.group(1))
    except Exception:
        return None

    # Walk the JSON to find something that looks like submissionDetails
    def walk(node):
        if isinstance(node, dict):
            # direct hit
            if "code" in node and "lang" in node and isinstance(node.get("question"), dict) and "questionId" in node["question"]:
                return node
            for v in node.values():
                r = walk(v)
                if r: return r
        elif isinstance(node, list):
            for v in node:
                r = walk(v)
                if r: return r
        return None

    return walk(j)

def fetch_submission_via_html(session: requests.Session, sub_id: int) -> Optional[Dict[str, Any]]:
    url = f"https://leetcode.com/submissions/detail/{sub_id}/"
    hdrs = {
        "User-Agent": "Mozilla/5.0 (compatible; GitHubActionsBot/1.0)",
        "Referer": f"https://leetcode.com/submissions/detail/{sub_id}/",
        "Origin": "https://leetcode.com",
    }
    r = session.get(url, headers=hdrs, timeout=45)
    if r.status_code == 404:
        return None
    r.raise_for_status()
    data = extract_from_next_data(r.text)
    if not data:
        return None
    # Normalize to graphQL-like shape
    return {
        "code": data.get("code"),
        "lang": data.get("lang"),
        "question": {"questionId": str(data.get("question", {}).get("questionId"))}
    }

def robust_fetch_submission(session: requests.Session, sub_id: int) -> Optional[Dict[str, Any]]:
    # Try GraphQL first
    try:
        det = fetch_submission_via_graphql(session, sub_id)
        if det and det.get("code"):
            return det
    except Exception as e:
        # Only fall back on known headless-unfriendly statuses
        msg = str(e)
        if "HTTP 400" not in msg and "HTTP 403" not in msg:
            # unknown hard failure; surface as None (skip)
            pass
    # Fallback: HTML page parse
    try:
        det = fetch_submission_via_html(session, sub_id)
        if det and det.get("code"):
            return det
    except Exception:
        pass
    return None

def main():
    root = pathlib.Path(".").resolve()
    session = make_session()

    items = fetch_recent(session, USERNAME, limit=20)
    if not items:
        print("No recent accepted submissions. Nothing to do.")
        return

    saved = 0
    for it in items:
        sid = it.get("id")
        if not sid:
            continue
        try:
            sid = int(sid)
        except Exception:
            continue

        det = robust_fetch_submission(session, sid)
        if not det:
            print(f"Skip sub {sid}: unable to retrieve details (GraphQL/HTML).")
            continue

        q = det.get("question") or {}
        qnum = q.get("questionId")
        lang_key = (det.get("lang") or "").lower()
        code = det.get("code") or ""
        ext = LANG_EXT.get(lang_key)

        if not (qnum and ext and code):
            print(f"Skip sub {sid}: missing qnum/ext/code.")
            continue

        dest = root / f"{qnum}.{ext}"
        if dest.exists() and dest.read_text(encoding="utf-8") == code:
            continue
        dest.write_text(code, encoding="utf-8")
        saved += 1

    print(f"Saved/updated {saved} files.")

if __name__ == "__main__":
    main()
