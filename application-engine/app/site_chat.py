"""In-app coding assistant: lets the candidate ask for site changes from the
browser instead of a terminal. Every change is PROPOSED, not applied — the
model reads real files via tools, drafts a complete replacement for any file
it wants to change, and that sits as a diff until a human clicks Apply. This
module never writes to disk on its own; only `apply_edit` does, and only for
a specific already-proposed row.

Sandboxed to the project's own code: data/ (resume, application DB, uploads),
.env (API keys), and .venv/ are off-limits to both read_file and propose_edit,
regardless of what's asked — the assistant is here to edit the app, not to
see personal data or credentials.
"""

import difflib
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from app.llm import MODEL_SONNET, get_client, log_usage

PROJECT_ROOT = Path(__file__).resolve().parent.parent
MAX_TOOL_ITERATIONS = 15
MAX_READ_CHARS = 20000
MAX_LIST_ENTRIES = 500

_DENYLIST_TOP_LEVEL = {"data", ".venv", "__pycache__", "node_modules"}
_SKIP_DIR_NAMES = {".venv", "__pycache__", "node_modules", "data", ".git"}

SYSTEM_PROMPT = """You are the in-app coding assistant for this personal job-application-automation
project (FastAPI + Jinja2, Python backend, SQLite database). The person you're talking to had an
external coding assistant build this app and now wants to make small-to-medium improvements
themselves, from the browser, without going back to a terminal.

How you work:
- Use list_files and read_file to look at the actual current code before proposing any change —
  never guess at file contents or write a change blind.
- Use propose_edit to suggest a change. This does NOT modify anything on disk — it only queues a
  proposed change that the person will review as a diff and explicitly click Apply on. Nothing you
  do is live until they approve it. Propose edits to as many files as the request actually needs.
- new_content in propose_edit must be the COMPLETE new file content, not a fragment or diff — read
  the file first and edit from its real current content. For a brand-new file, just write the full
  content directly.
- Off-limits, and sandboxed away from these tools entirely: data/ (resume, application database,
  uploaded files, generated docs), .env (API keys/credentials), .venv/. Don't attempt to read or
  write anything there.
- Keep changes scoped to what was actually asked — don't refactor unrelated code, don't add comments
  explaining what code does, don't add error handling for cases that can't happen. Match the existing
  code style you see when you read files (plain functions, minimal comments, no premature
  abstraction).
- After proposing edits, explain in plain language what you changed and why, and mention if changes
  to .py files need the server restarted to take effect (Jinja templates and static files take
  effect immediately on the next request; .py route/logic changes need a restart — none needed if
  the server is running with uvicorn's --reload flag, since it restarts itself on file changes).
- If a request is ambiguous or could reasonably be done multiple ways, ask a clarifying question
  instead of guessing.
"""

TOOLS = [
    {
        "name": "list_files",
        "description": (
            "List files and directories under a path within the project, relative to the project "
            "root. Excludes data/, .venv/, __pycache__/, and other off-limits paths."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Relative path from the project root, e.g. 'app/web/templates'. Use '.' for the root.",
                }
            },
            "required": ["path"],
            "additionalProperties": False,
        },
        "strict": True,
    },
    {
        "name": "read_file",
        "description": "Read the full text content of a file within the project, relative to the project root.",
        "input_schema": {
            "type": "object",
            "properties": {"path": {"type": "string", "description": "Relative path from the project root."}},
            "required": ["path"],
            "additionalProperties": False,
        },
        "strict": True,
    },
    {
        "name": "propose_edit",
        "description": (
            "Propose writing new_content as the complete new contents of the file at path (relative "
            "to the project root). This does NOT write to disk — it only records a proposed change "
            "for human review. Always read the current file first (unless it's a brand-new file) so "
            "new_content is a complete, correct replacement, not a partial snippet."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Relative path from the project root."},
                "new_content": {"type": "string", "description": "The complete new file content."},
                "explanation": {"type": "string", "description": "One or two sentences on what this change does and why."},
            },
            "required": ["path", "new_content", "explanation"],
            "additionalProperties": False,
        },
        "strict": True,
    },
]


class SandboxViolation(Exception):
    pass


def _resolve_safe_path(rel_path: str) -> Path:
    """Resolve a model-supplied relative path and confirm it stays inside
    the project root and outside the denylist. Raises SandboxViolation
    rather than silently clamping — a rejected path should read back to the
    model as a clear refusal, not a surprising substitution."""
    candidate = (PROJECT_ROOT / rel_path).resolve()
    if candidate != PROJECT_ROOT and PROJECT_ROOT not in candidate.parents:
        raise SandboxViolation(f"'{rel_path}' resolves outside the project root.")
    rel = candidate.relative_to(PROJECT_ROOT)
    if rel.parts and rel.parts[0] in _DENYLIST_TOP_LEVEL:
        raise SandboxViolation(f"'{rel_path}' is off-limits (under {rel.parts[0]}/).")
    if any(part.startswith(".env") for part in rel.parts):
        raise SandboxViolation(f"'{rel_path}' is off-limits (env/credentials file).")
    return candidate


def _tool_list_files(rel_path: str) -> str:
    try:
        base = _resolve_safe_path(rel_path)
    except SandboxViolation as exc:
        return f"Error: {exc}"
    if not base.exists():
        return f"Error: '{rel_path}' does not exist."
    if base.is_file():
        return f"'{rel_path}' is a file, not a directory."

    entries = []
    for p in sorted(base.rglob("*")):
        rel_parts = p.relative_to(PROJECT_ROOT).parts
        if any(part in _SKIP_DIR_NAMES for part in rel_parts[:-1]):
            continue
        if p.name in _SKIP_DIR_NAMES or p.name.startswith(".env"):
            continue
        entries.append(str(p.relative_to(PROJECT_ROOT)) + ("/" if p.is_dir() else ""))
        if len(entries) >= MAX_LIST_ENTRIES:
            entries.append(f"... truncated at {MAX_LIST_ENTRIES} entries")
            break
    return "\n".join(entries) if entries else "(empty)"


def _tool_read_file(rel_path: str) -> str:
    try:
        path = _resolve_safe_path(rel_path)
    except SandboxViolation as exc:
        return f"Error: {exc}"
    if not path.exists() or not path.is_file():
        return f"Error: '{rel_path}' is not an existing file."
    text = path.read_text(errors="replace")
    if len(text) > MAX_READ_CHARS:
        return text[:MAX_READ_CHARS] + f"\n\n... truncated at {MAX_READ_CHARS} characters, file is longer"
    return text


def _tool_propose_edit(rel_path: str, new_content: str, explanation: str) -> tuple[str, dict | None]:
    try:
        path = _resolve_safe_path(rel_path)
    except SandboxViolation as exc:
        return f"Error: {exc}", None

    old_content = path.read_text(errors="replace") if path.exists() and path.is_file() else None
    diff = "\n".join(
        difflib.unified_diff(
            (old_content or "").splitlines(),
            new_content.splitlines(),
            fromfile=f"a/{rel_path}",
            tofile=f"b/{rel_path}",
            lineterm="",
        )
    )
    edit = {
        "file_path": str(path.relative_to(PROJECT_ROOT)),
        "old_content": old_content,
        "new_content": new_content,
        "diff": diff or "(no textual difference)",
        "explanation": explanation,
    }
    return f"Proposed edit for '{rel_path}' recorded — awaiting human review, not applied yet.", edit


def _execute_tool(name: str, tool_input: dict) -> tuple[str, dict | None]:
    if name == "list_files":
        return _tool_list_files(tool_input.get("path", ".")), None
    if name == "read_file":
        return _tool_read_file(tool_input.get("path", "")), None
    if name == "propose_edit":
        return _tool_propose_edit(
            tool_input.get("path", ""), tool_input.get("new_content", ""), tool_input.get("explanation", "")
        )
    return f"Unknown tool: {name}", None


def run_chat_turn(conn: sqlite3.Connection, user_text: str) -> dict:
    """Run one turn: persist the user message, loop the model against the
    read/propose tools until it stops calling tools, persist its final text
    reply, and attach any proposed edits to that reply. Returns
    {"assistant_message_id": int, "text": str, "proposed_edits": [...]}."""
    now = datetime.now(timezone.utc).isoformat()
    conn.execute("INSERT INTO chat_messages (role, content, created_at) VALUES ('user', ?, ?)", (user_text, now))
    conn.commit()

    history = conn.execute("SELECT role, content FROM chat_messages ORDER BY id").fetchall()
    messages = [{"role": r["role"], "content": r["content"]} for r in history]

    client = get_client()
    collected_edits: list[dict] = []
    final_text = "(No response.)"

    for _ in range(MAX_TOOL_ITERATIONS):
        response = client.messages.create(
            model=MODEL_SONNET,
            max_tokens=4096,
            system=SYSTEM_PROMPT,
            tools=TOOLS,
            messages=messages,
        )
        log_usage(conn, "site_chat", MODEL_SONNET, response.usage)

        if response.stop_reason != "tool_use":
            final_text = "".join(b.text for b in response.content if b.type == "text") or "(No response.)"
            break

        messages.append({"role": "assistant", "content": response.content})
        tool_results = []
        for block in response.content:
            if block.type != "tool_use":
                continue
            result_text, edit = _execute_tool(block.name, block.input)
            if edit:
                collected_edits.append(edit)
            tool_results.append({"type": "tool_result", "tool_use_id": block.id, "content": result_text})
        messages.append({"role": "user", "content": tool_results})
    else:
        final_text = "(Stopped after reaching this turn's tool-call limit — ask me to continue if needed.)"

    now2 = datetime.now(timezone.utc).isoformat()
    cursor = conn.execute(
        "INSERT INTO chat_messages (role, content, created_at) VALUES ('assistant', ?, ?)", (final_text, now2)
    )
    assistant_message_id = cursor.lastrowid

    for edit in collected_edits:
        conn.execute(
            """
            INSERT INTO chat_proposed_edits
                (message_id, file_path, old_content, new_content, diff, status, created_at)
            VALUES (?, ?, ?, ?, ?, 'PENDING', ?)
            """,
            (assistant_message_id, edit["file_path"], edit["old_content"], edit["new_content"], edit["diff"], now2),
        )
    conn.commit()

    return {"assistant_message_id": assistant_message_id, "text": final_text, "proposed_edits": collected_edits}


def apply_edit(conn: sqlite3.Connection, edit_id: int) -> dict:
    row = conn.execute("SELECT * FROM chat_proposed_edits WHERE id = ?", (edit_id,)).fetchone()
    if row is None:
        return {"ok": False, "error": "No such proposed edit."}
    if row["status"] != "PENDING":
        return {"ok": False, "error": f"Already {row['status']}."}

    try:
        path = _resolve_safe_path(row["file_path"])
    except SandboxViolation as exc:
        return {"ok": False, "error": str(exc)}

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(row["new_content"])

    now = datetime.now(timezone.utc).isoformat()
    conn.execute(
        "UPDATE chat_proposed_edits SET status = 'APPLIED', applied_at = ? WHERE id = ?", (now, edit_id)
    )
    conn.commit()
    return {"ok": True, "file_path": row["file_path"]}


def discard_edit(conn: sqlite3.Connection, edit_id: int) -> None:
    conn.execute("UPDATE chat_proposed_edits SET status = 'DISCARDED' WHERE id = ? AND status = 'PENDING'", (edit_id,))
    conn.commit()


def get_history(conn: sqlite3.Connection) -> list[dict]:
    """Every message, with its proposed edits (if any) attached — enough
    for the chat page to render the full transcript in one query pass."""
    messages = [dict(r) for r in conn.execute("SELECT * FROM chat_messages ORDER BY id").fetchall()]
    edits = conn.execute("SELECT * FROM chat_proposed_edits ORDER BY id").fetchall()
    by_message: dict[int, list[dict]] = {}
    for e in edits:
        by_message.setdefault(e["message_id"], []).append(dict(e))
    for m in messages:
        m["edits"] = by_message.get(m["id"], [])
    return messages
