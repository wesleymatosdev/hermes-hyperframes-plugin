"""h_lint success semantics: error counts, never substring matching.

Found in live verification of the security round: the real CLI's summary
("◇ 0 error(s), 4 warning(s)") always contains the literal ``error(s)``, so
the old ``"error(s)" not in last_line`` check made every clean lint report
``success: false``.
"""

from __future__ import annotations

import json
import os
import re
import stat
import unittest
from pathlib import Path

from support import DashboardTestBase

_LINT_BIN = r"""#!/usr/bin/env python3
import sys
# argv[1:] beyond 'lint' is unused here; print the case named by $LINT_CASE.
case = __import__('os').environ.get('LINT_CASE', 'clean')
if case == 'clean':
    print("◇  0 error(s), 4 warning(s)")
elif case == 'errors':
    print("⚠ [comp.html] something_broke: 2 error(s)")
    print("◇  2 error(s), 0 warning(s)")
    raise SystemExit(1)
"""


class LintSuccessTests(DashboardTestBase):
    def _project_with_case(self, name: str, case: str) -> str:
        project = self.make_fake_project(name)
        stub = Path(project) / "node_modules" / ".bin" / "hyperframes"
        stub.write_text(_LINT_BIN, encoding="utf-8")
        stub.chmod(stub.stat().st_mode | stat.S_IEXEC)
        # LINT_CASE rides to the stub through env_passthrough (also proves the
        # opt-in passthrough knob works end to end).
        import hermes_cli.config as hc  # noqa: F401

        from hermes_constants import get_hermes_home  # type: ignore

        home = Path(get_hermes_home())
        config = {"plugins": {"entries": {"hermes-hyperframes": {"settings": {
            "env_passthrough": ["LINT_CASE"]}}}}}
        (home / "config.yaml").write_text(json.dumps(config), encoding="utf-8")
        os.environ["LINT_CASE"] = case
        return str(project)

    def test_clean_lint_with_warning_text_is_success(self) -> None:
        project = self._project_with_case("clean-cut", "clean")
        result = json.loads(self.api._TOOLS.h_lint({"project_dir": project}))
        self.assertTrue(result["success"], result)

    def test_error_count_fails_lint(self) -> None:
        project = self._project_with_case("broken-cut", "errors")
        result = json.loads(self.api._TOOLS.h_lint({"project_dir": project}))
        self.assertFalse(result["success"])


if __name__ == "__main__":
    unittest.main()
