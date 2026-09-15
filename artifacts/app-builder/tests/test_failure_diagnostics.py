"""Compiler repair must see root errors, not only Cargo's final command line."""
import os
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from common import PipelineError, ProcessLimits, bounded_failure_diagnostic, run_bounded


class FailureDiagnosticTests(unittest.TestCase):
    def test_short_output_is_preserved(self):
        text = b"error[E0432]: unresolved import\n  --> src/lib.rs:4:5\n"
        self.assertEqual(bounded_failure_diagnostic(text), text.decode())

    def test_long_cargo_output_keeps_first_error_and_terminal_summary(self):
        stderr = (b"   Compiling dependency\n" * 600
                  + b"error[E0432]: unresolved import ROOT_CAUSE\n  --> src/lib.rs:4:5\n"
                  + b"error[E0308]: repeated follow-on diagnostic\n" * 900
                  + b"could not compile app due to 70 previous errors\n")
        result = bounded_failure_diagnostic(stderr)
        self.assertIn("ROOT_CAUSE", result)
        self.assertIn("src/lib.rs:4:5", result)
        self.assertIn("70 previous errors", result)
        self.assertIn("omitted", result)
        self.assertLessEqual(len(result.encode()), 12000)

    def test_non_compiler_failure_retains_both_ends(self):
        result = bounded_failure_diagnostic(b"FIRST_FAILURE\n" + b"x" * 50000 + b"\nLAST_FAILURE")
        self.assertIn("FIRST_FAILURE", result)
        self.assertIn("LAST_FAILURE", result)
        self.assertLessEqual(len(result.encode()), 12000)

    def test_invalid_utf8_does_not_break_the_byte_bound(self):
        result = bounded_failure_diagnostic(b"\xff" * 50000)
        self.assertLessEqual(len(result.encode()), 12000)

    def test_real_failing_process_exposes_first_error(self):
        with tempfile.TemporaryDirectory(prefix="vibapp-diagnostic-test-") as directory:
            root = Path(directory)
            code = ("import sys; sys.stderr.write('error[E0432]: ROOT_CAUSE\\n'"
                    " + 'follow-on error\\n' * 2000 + 'FINAL_SUMMARY\\n'); sys.exit(1)")
            with self.assertRaises(PipelineError) as caught:
                run_bounded([sys.executable, "-c", code], cwd=root,
                            environment={"PATH": os.defpath}, limits=ProcessLimits(), disk_root=root)
            self.assertEqual(caught.exception.code, "process-failed")
            self.assertIn("ROOT_CAUSE", str(caught.exception))
            self.assertIn("FINAL_SUMMARY", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
