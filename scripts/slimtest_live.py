#!/usr/bin/env python3
"""Opt-in REAL calls to the operator-authorized loopback slimtest bot only.

No model response is faked. This driver never changes gateway configuration or
processes. The sole credential read is the explicitly authorized slimtest key.
Runtime receipts/state are kept in the ignored project .tmp directory.
"""
import argparse
import hashlib
import json
from pathlib import Path
import re
import time
import urllib.error
import urllib.request
import uuid

ROOT = Path(__file__).resolve().parents[1]
HOME = Path.home() / ".hermes-slimtest"
BASE = "http://127.0.0.1:8699"
STATE = ROOT / ".tmp" / "slimtest-live-state.json"


def emit(event, **fields):
    print(json.dumps({"event": event, "at": time.strftime("%Y-%m-%d %H:%M:%S"), **fields},
                     ensure_ascii=False), flush=True)


class Client:
    def __init__(self):
        self.key = (HOME / ".testkey").read_text().strip()

    def request(self, path, body=None, session=None):
        headers = {"Authorization": "Bearer " + self.key}
        if session:
            headers["X-Hermes-Session-Id"] = session
        if body is not None:
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(BASE + path,
            data=None if body is None else json.dumps(body).encode(), headers=headers)
        with urllib.request.urlopen(request, timeout=480) as reply:
            return json.load(reply)

    def messages(self, sid):
        return self.request(f"/api/sessions/{sid}/messages?limit=500&order=oldest")["data"]

    def turn(self, sid, text, diagnostic=True):
        body = {"model": "hermes-agent", "stream": False,
                "messages": [{"role": "user", "content": text}]}
        if diagnostic:
            body["hermes_notification_category"] = "diagnostic"
        started = time.monotonic()
        result = self.request("/v1/chat/completions", body, sid)
        choice = result["choices"][0]
        answer = choice["message"]["content"]
        emit("turn", sid=sid, seconds=round(time.monotonic() - started, 2),
             finish_reason=choice.get("finish_reason"), usage=result.get("usage"),
             reply=answer[:350])
        assert choice.get("finish_reason") == "stop", "bot turn did not finish normally"
        return answer, result


def prepare_fixtures():
    directory = ROOT / ".tmp" / "live-fixtures"
    directory.mkdir(parents=True, exist_ok=True)
    state = 73129451
    for f in range(64):
        rows = []
        for row in range(110):
            words = []
            for _ in range(12):
                letters = []
                for _ in range(12):
                    state = (state * 1664525 + 1013904223) & 0xffffffff
                    letters.append(chr(97 + state % 26))
                words.append("".join(letters))
            rows.append(f"{row:03d} " + " ".join(words))
        (directory / f"data-{f:02d}.txt").write_text("\n".join(rows) + "\n")


def prepare_detail_fixture():
    """A small file whose line 87 word exists ONLY in the archived tool output."""
    directory = ROOT / ".tmp" / "live-fixtures"
    directory.mkdir(parents=True, exist_ok=True)
    state = 991733
    rows = []
    for line in range(1, 121):
        words = []
        for _ in range(4):
            letters = []
            for _ in range(13):
                state = (state * 1103515245 + 12345) & 0x7fffffff
                letters.append(chr(97 + state % 26))
            words.append("".join(letters))
        rows.append(f"L{line}\t" + " ".join(words))
    marker = rows[86].split("\t", 1)[1].split()[0]
    path = directory / "detail-B.txt"
    path.write_text("\n".join(rows) + "\n")
    return path, marker


def summary_fingerprint(messages):
    # The REST presentation deliberately blanks hidden summary content. Its
    # newly persisted row id is still exposed and changes at each commit.
    summaries = [m for m in messages if m.get("role") == "user" and (
        "[CONTEXT COMPACTION" in str(m.get("content", "")) or m.get("display_kind") == "hidden")]
    if not summaries:
        return None
    marker = {key: summaries[-1].get(key) for key in ("id", "session_id", "content", "display_kind")}
    return hashlib.sha256(json.dumps(marker, sort_keys=True).encode()).hexdigest()


def parse_answer(answer):
    answer = answer.strip()
    fence = re.fullmatch(r"```(?:json)?\s*\n(.*?)\n```", answer, re.DOTALL | re.IGNORECASE)
    return json.loads(fence.group(1) if fence else answer)


def phase_archive(client):
    """Compaction must send the model to session_search for a fact only the archive holds."""
    prepare_fixtures()
    path, marker = prepare_detail_fixture()
    sid = "steward-archive-" + uuid.uuid4().hex[:10]
    client.request("/api/sessions", {"id": sid, "title": sid, "source": "api_server"})
    for i in range(2):
        client.turn(sid, f"Setup {i}: authorized retention test. Use only read_file. "
                    "Never edit, run shell, delegate or contact anyone. Reply READY.", diagnostic=False)
    client.turn(sid, "Read this file completely with read_file: " + str(path) +
                "\nDo not quote or summarize its contents; reply only READ and wait.")
    emit("detail_read", sid=sid, path=str(path), line=87, marker=marker)
    fingerprint = summary_fingerprint(client.messages(sid))
    batch = 0
    while batch < 14 and fingerprint is None:
        paths = [str(ROOT / ".tmp" / "live-fixtures" / f"data-{(batch*8+i)%64:02d}.txt") for i in range(8)]
        client.turn(sid, "FILLER request: read these eight files with read_file, one call each, "
                    "keeping the output in this conversation; reply only ACK and wait.\n" + "\n".join(paths))
        batch += 1
        fingerprint = summary_fingerprint(client.messages(sid))
    emit("compacted", sid=sid, filler_batches=batch, fingerprint=fingerprint)
    assert fingerprint is not None, "no compaction observed"
    question = ("Question about work earlier in THIS session: in detail-B.txt that you read before, "
                 "what is the first word on line 87? Answer with that word only — do not guess; "
                 "if the detail is not in your current context, look it up in the archived history.")
    answer, _ = client.turn(sid, question)
    passed = marker in answer
    emit("archive_recall", sid=sid, expected=marker, observed=answer[:200], passed=passed)
    STATE.write_text(json.dumps({"sid": sid, "marker": marker, "filler_batches": batch},
                                ensure_ascii=False, indent=2))
    assert passed, "bot did not recover the archived line-87 word"
    emit("completed", phase="archive", sid=sid, compactions=1, filler_batches=batch)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--phase", choices=("normal", "fallback", "archive"), required=True)
    parser.add_argument("--resume", action="store_true", help="continue existing fixture session; count only new commits")
    args = parser.parse_args()
    client = Client()
    if args.phase == "archive":
        phase_archive(client)
        return
    if args.phase == "normal" and not args.resume:
        prepare_fixtures()
        sid = "steward-extreme-" + uuid.uuid4().hex[:10]
        client.request("/api/sessions", {"id": sid, "title": sid, "source": "api_server"})
        state = {"sid": sid, "compactions": 0, "next_batch": 0, "fingerprint": None,
                 "verifications": [], "phase": "normal"}
        for i in range(3):
            client.turn(sid, f"Setup {i}: This is an authorized context-retention test. "
                        "Use only read_file on the supplied fixture paths. Never edit files, run shell, "
                        "delegate, or contact anyone. Reply READY and wait.", diagnostic=False)
    else:
        state = json.loads(STATE.read_text())
        sid = state["sid"]
        state["phase"] = args.phase
        if args.phase == "normal":
            state["compactions"] = 0
            state["verifications"] = []
            state["fingerprint"] = summary_fingerprint(client.messages(sid))
    emit("started", phase=args.phase, sid=sid, baseline_summary=state["fingerprint"])
    rounds = 14 if args.phase == "normal" else 5
    for _ in range(rounds):
        batch = state["next_batch"]
        details = {"request_id": f"REQ-{batch:02d}-{uuid.uuid4().hex[:8]}",
                   "project": f"玄武岩-{batch}-Orion", "deadline": f"2031-09-{10+batch:02d}",
                   "code_line": f"assert shard == {batch * 17 + 3}"}
        paths = [str(ROOT / ".tmp" / "live-fixtures" / f"data-{(batch*8+i)%64:02d}.txt")
                 for i in range(8)]
        text = (
            "DATA request: " + json.dumps(details, ensure_ascii=False) + "\n"
            "Read all eight files below with read_file, one call per file, preserving all available "
            "file output in this conversation. Do not use terminal or delegate and do not write files. "
            "The pending task is to inspect these files; after doing so reply only ACK with request_id "
            "and wait for my verification question.\n" + "\n".join(paths)
        )
        client.turn(sid, text)
        active = client.messages(sid)
        fingerprint = summary_fingerprint(active)
        exact = any(m.get("role") == "user" and m.get("content") == text for m in active)
        emit("batch", phase=args.phase, batch=batch, rows=len(active), latest_request_exact=exact,
             request_sha256=hashlib.sha256(text.encode()).hexdigest(), summary=fingerprint)
        assert exact, "latest DATA request disappeared from active user rows"
        if fingerprint and fingerprint != state["fingerprint"]:
            state["compactions"] += 1
            state["fingerprint"] = fingerprint
            question = (
                "VERIFY: Without reading any file or using tools, return ONLY the JSON object with "
                "request_id, project, deadline, code_line from my immediately preceding DATA request. "
                "Use the original exact values; do not guess."
            )
            answer, _ = client.turn(sid, question)
            observed = parse_answer(answer)
            passed = observed == details
            state["verifications"].append({"compaction": state["compactions"], "expected": details,
                                             "observed": observed, "passed": passed})
            emit("verified", compaction=state["compactions"], passed=passed,
                 expected=details, observed=observed)
            STATE.write_text(json.dumps(state, ensure_ascii=False, indent=2))
            assert passed, "real bot failed exact recall after compaction"
        state["next_batch"] = batch + 1
        STATE.write_text(json.dumps(state, ensure_ascii=False, indent=2))
        if args.phase == "normal" and state["compactions"] >= 2:
            emit("completed", phase=args.phase, sid=sid, compactions=state["compactions"],
                 verifications=state["verifications"])
            return
        if args.phase == "fallback" and state["compactions"] >= 3:
            emit("completed", phase=args.phase, sid=sid, compactions=state["compactions"])
            return
    emit("incomplete", phase=args.phase, sid=sid, compactions=state["compactions"])
    raise SystemExit(1)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        emit("failed", error_type=type(exc).__name__, reason=str(exc)[:250])
        raise SystemExit(1)
