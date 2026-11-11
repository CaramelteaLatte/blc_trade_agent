import json
import os
import shutil
import subprocess
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from onchain_agent.data_tools import ROOT
from onchain_agent.progress import ProgressFeed, record_progress


class ProgressTests(unittest.TestCase):
    def test_mcp_progress_is_delivered_before_context_exit_and_redacted(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "progress.jsonl"
            seen, delivered = [], threading.Event()
            def emit(message):
                seen.append(message)
                delivered.set()
            with patch.dict(os.environ, {"RESEARCH_PROGRESS_LOG": str(path), "OPENAI_API_KEY": "private-key"}):
                with ProgressFeed(path, emit):
                    record_progress("采集进度 private-key")
                    self.assertTrue(delivered.wait(2), "Progress was buffered until completion")
            self.assertEqual(seen, ["采集进度 [REDACTED]"])
            self.assertNotIn("private-key", path.read_text())

    def test_partial_and_invalid_progress_lines_do_not_interrupt_reporting(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "progress.jsonl"
            seen = []
            with ProgressFeed(path, seen.append):
                with path.open("a") as out:
                    out.write('invalid\n{"message":"ready')
                with path.open("a") as out:
                    out.write('"}\n')
            self.assertEqual(seen, ["ready"])

    @unittest.skipUnless(shutil.which("node"), "Node is needed only for this bridge unit check")
    def test_bridge_forwards_summary_frames_and_honors_hide_switch(self):
        script = """
import { apply } from './.dsh/progress-bridge.mjs';
let listener;
apply({ on(name, callback, options) {
  if (!options?.global) throw new Error('SDK-created agents require a global listener');
  if (name === 'agent/assistant-stream') listener = callback;
} });
listener({frame:{type:'start',attemptId:'test'}});
for (const chunk of [
  {type:'reasoning-delta',text:'Checking the '},
  {type:'reasoning-delta',text:'data.\\n'},
  {type:'text-delta',text:'private report JSON'},
  {type:'tool-call-delta',argumentsDelta:'private arguments'},
]) listener({frame:{type:'chunk',attemptId:'test',chunk}});
listener({frame:{type:'end',attemptId:'test'}});
"""
        with tempfile.TemporaryDirectory() as folder:
            for enabled in ("0", "1"):
                path = Path(folder) / enabled
                env = {**os.environ, "RESEARCH_PROGRESS_LOG": str(path), "RESEARCH_SHOW_REASONING": enabled}
                subprocess.run(["node", "--input-type=module", "-e", script], cwd=ROOT, env=env,
                               check=True, capture_output=True, timeout=10)
                content = path.read_text()
                self.assertEqual("Checking the data." in content, enabled == "1")
                self.assertNotIn("private", content)
                self.assertTrue(all(isinstance(json.loads(line)["message"], str) for line in content.splitlines()))
