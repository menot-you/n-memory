"""Durability proof test across process lifetimes against the SQLite store.

Acceptance:
- Ingest via stdio server process 1.
- Terminate process 1 completely.
- Start new stdio server process 2 against the same store file.
- Retrieve the same capsule by ID (nmemory_capsule_show) and by search (nmemory_capsule_retrieve).
- Exit 0.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time

from child_env import child_env
from nott_database import DatabaseError
from stdio_peer import INITIALIZED, complete
from storefile import default_store_path


class StdioClient:
    def __init__(self, env: dict[str, str] | None = None) -> None:
        merged_env = child_env(dict(os.environ, **(env or {})))
        merged_env["PYTHONPATH"] = os.path.abspath(
            os.path.join(os.path.dirname(__file__), "..")
        )
        self.proc = subprocess.Popen(
            [sys.executable, "-m", "nmemory", "mcp", "serve"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=merged_env,
            text=True,
            bufsize=1,
        )
        self.msg_id = 0
        # The child writes diagnostics to stderr. A PIPE nobody reads fills
        # up and BLOCKS the child mid-write — it never gets to respond on
        # stdout, and this process waits forever in readline(). Measured on
        # this host: 9781 open pipes exceed fs/pipe-user-pages-soft, so the
        # kernel creates ONE-PAGE pipes (4096 B) instead of 65536, and the
        # server jams after ~4 KB of the 16591 it emits. On an unloaded
        # machine the same code passes: the defect is latent and the host
        # reveals it.
        self._err_chunks: list[str] = []
        self._err_thread = threading.Thread(
            target=self._drain_stderr, daemon=True, name="stdio-stderr-drain"
        )
        self._err_thread.start()

    def _drain_stderr(self) -> None:
        """Continuously drains stderr so the pipe never fills up."""
        stream = self.proc.stderr
        if stream is None:
            return
        for line in stream:
            self._err_chunks.append(line)

    def _stderr_text(self) -> str:
        return "".join(self._err_chunks)

    def send(self, method: str, params: dict | None = None) -> dict:
        self.msg_id += 1
        payload = complete(
            {"id": self.msg_id, "method": method, "params": params or {}}
        )
        line = json.dumps(payload) + "\n"
        assert self.proc.stdin is not None
        assert self.proc.stdout is not None
        self.proc.stdin.write(line)
        self.proc.stdin.flush()

        resp_line = self.proc.stdout.readline()
        if not resp_line:
            assert self.proc.stderr is not None
            err = self._stderr_text()
            raise RuntimeError(f"server process died: {err}")
        if method == "initialize":
            self.proc.stdin.write(json.dumps(INITIALIZED) + "\n")
            self.proc.stdin.flush()
        return json.loads(resp_line.strip())

    def call_tool(self, name: str, arguments: dict | None = None) -> dict:
        return self.send("tools/call", {"name": name, "arguments": arguments or {}})

    def terminate_and_wait(self) -> None:
        if self.proc.poll() is None:
            if self.proc.stdin:
                try:
                    self.proc.stdin.close()
                except OSError:
                    pass
            try:
                self.proc.wait(timeout=3)
            except subprocess.TimeoutExpired, OSError:
                self.proc.kill()
                self.proc.wait(timeout=3)


def test_durability_across_process_lifetimes() -> None:
    print(f"=== Durability Proof Test ({default_store_path()}) ===")

    env: dict[str, str] = {}  # the children inherit the test's NOTT_DATABASE_DIR

    test_content = "SQLite durability test verification token 987654321."
    test_project = "durability-project"

    # --- LIFECYCLE 1: Server Process 1 ---
    print("\n--- Phase 1: Launch Server Process 1 & Ingest Capsule ---")
    client1 = StdioClient(env=env)
    cap_id: str = ""
    try:
        init_res1 = client1.send("initialize", {"protocolVersion": "2024-11-05"})
        assert "result" in init_res1, f"Process 1 init failed: {init_res1}"
        print("✓ Server Process 1 initialized")

        ingest_res = client1.call_tool(
            "nmemory_capsule_ingest",
            {
                "items": [
                    {
                        "content": test_content,
                        "source": "durability:test",
                        "anchor": "TEST_DURABILITY.md:10",
                        "confidence": 0.95,
                        "project_id": test_project,
                        "kind": "fact",
                    }
                ]
            },
        )
        assert "result" in ingest_res, f"Ingest failed: {ingest_res}"
        data = json.loads(ingest_res["result"]["content"][0]["text"])
        assert data["outcomes"][0]["status"] == "captured"
        cap_id = data["outcomes"][0]["id"]
        print(f"✓ Ingested capsule {cap_id} in Server Process 1")
    finally:
        print("\n--- Phase 2: Terminate Server Process 1 ---")
        client1.terminate_and_wait()
        assert client1.proc.poll() is not None, "Process 1 failed to terminate"
        print(f"✓ Server Process 1 is dead (exit code: {client1.proc.poll()})")

    # Small pause to guarantee process boundary
    time.sleep(0.1)

    # --- LIFECYCLE 2: Server Process 2 ---
    print("\n--- Phase 3: Launch NEW Server Process 2 & Retrieve Capsule ---")
    client2 = StdioClient(env=env)
    try:
        init_res2 = client2.send("initialize", {"protocolVersion": "2024-11-05"})
        assert "result" in init_res2, f"Process 2 init failed: {init_res2}"
        print("✓ Server Process 2 initialized against the same store file")

        # 1. Retrieve by ID (nmemory_capsule_show)
        print(f"Retrieving capsule by ID {cap_id} via nmemory_capsule_show...")
        get_res = client2.call_tool("nmemory_capsule_show", {"id": cap_id})
        assert "result" in get_res, f"nmemory_capsule_show failed: {get_res}"
        get_data = json.loads(get_res["result"]["content"][0]["text"])
        assert get_data["id"] == cap_id
        assert get_data["capsule"]["content"] == test_content
        print(
            f"✓ nmemory_capsule_show OK in Process 2: verified content = {get_data['capsule']['content']!r}"
        )

        # 2. Retrieve by Search (nmemory_capsule_retrieve)
        print(
            "Retrieving capsule by search query 'token 987654321' via nmemory_capsule_retrieve..."
        )
        recall_res = client2.call_tool(
            "nmemory_capsule_retrieve",
            {
                "terms": ["token", "987654321"],
                "project_id": test_project,
            },
        )
        assert "result" in recall_res, f"nmemory_capsule_retrieve failed: {recall_res}"
        recall_data = json.loads(recall_res["result"]["content"][0]["text"])
        assert recall_data["outcome"] == "grounded"
        assert len(recall_data["results"]) >= 1
        assert recall_data["results"][0]["id"] == cap_id
        assert "987654321" in recall_data["results"][0]["headline"]
        print(
            f"✓ nmemory_capsule_retrieve OK in Process 2: found grounded result with receipt {recall_data['receipt_id']}"
        )

        # 3. Post-Recovery Readback (nmemory_digest_show)
        print("Verifying post-recovery readback via nmemory_digest_show...")
        digest_res = client2.call_tool("nmemory_digest_show")
        assert "result" in digest_res, f"nmemory_digest_show failed: {digest_res}"
        digest_data = json.loads(digest_res["result"]["content"][0]["text"])
        assert digest_data["total"] >= 1
        newest_ids = [entry["id"] for entry in digest_data.get("newest", [])]
        assert cap_id in newest_ids, (
            f"Expected {cap_id} in newest digest entries: {newest_ids}"
        )
        matched_entry = next(
            entry for entry in digest_data["newest"] if entry["id"] == cap_id
        )
        assert "987654321" in matched_entry["headline"], (
            f"Expected token in headline: {matched_entry}"
        )
        print(
            f"✓ nmemory_digest_show post-recovery readback OK: digest names capsule {cap_id} ({matched_entry['headline']!r})"
        )

        print("\n=======================================================")
        print("DURABILITY PROOF PASSED: Cross-process persistence verified!")
        print("=======================================================")
    finally:
        client2.terminate_and_wait()


def main() -> int:
    try:
        test_durability_across_process_lifetimes()
        return 0
    except DatabaseError as e:
        sys.stderr.write(f"REFUSAL: {e}\n")
        return 2


if __name__ == "__main__":
    sys.exit(main())
