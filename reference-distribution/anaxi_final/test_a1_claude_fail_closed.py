"""
Anaxi -- Amendment A1 (Scoped Production Activation) fail-closed
regression suite. Proves each of the three A1.3.1-enumerated Claude-
generation executables (anaxi_final/claude_anaxi.py,
anaxi_final/claude_anaxi_battery.py, repo-root claude_agent.py) refuses
before any generation or persistence: zero AnaxiOrchestrator
construction, zero anthropic.Anthropic() construction/API invocation,
zero file written anywhere. Uses only mocks and a throwaway temporary
CWD -- no real API key, no real Claude call, no real persistence
surface touched, exactly as A1.3's own guards require.

Also proves module import itself remains permitted (A1.3: "Module
import remains permitted") -- only invocation of the guarded entry
points is refused.

Run:
    python test_a1_claude_fail_closed.py
"""

import os
import shutil
import sys
import tempfile
import types


def run_test_suite():
    results = []

    def check(name, cond):
        print(f"{'PASS' if cond else 'FAIL'}: {name}")
        results.append(cond)

    original_cwd = os.getcwd()
    # Isolated throwaway CWD: if the guard were ever bypassed
    # (regression), any file a leaking main() might write lands here,
    # never in a real project directory -- and the emptiness check
    # below would catch it either way.
    test_dir = tempfile.mkdtemp(prefix="anaxi_test_a1_fail_closed_")
    os.chdir(test_dir)

    try:
        # A poisoned AnaxiOrchestrator that fails the test outright if
        # ever constructed -- proves the guard fires strictly BEFORE
        # orchestrator construction, not merely before the API call.
        fake_orch_module = types.ModuleType("orchestration")

        class PoisonedOrchestrator:
            def __init__(self, *a, **k):
                raise AssertionError(
                    "AnaxiOrchestrator was constructed -- the A1 guard did not fire first."
                )
        fake_orch_module.AnaxiOrchestrator = PoisonedOrchestrator
        sys.modules["orchestration"] = fake_orch_module

        # A poisoned anthropic.Anthropic that fails the test outright
        # if ever instantiated -- proves zero API invocation.
        fake_anthropic_module = types.ModuleType("anthropic")

        class PoisonedAnthropic:
            def __init__(self, *a, **k):
                raise AssertionError(
                    "anthropic.Anthropic() was constructed -- the A1 guard did not fire first."
                )
        fake_anthropic_module.Anthropic = PoisonedAnthropic
        sys.modules["anthropic"] = fake_anthropic_module

        # Deliberately present (not absent) so a would-be regression
        # can't accidentally "pass" by tripping the pre-existing
        # ANTHROPIC_API_KEY check instead of the A1 guard.
        original_api_key = os.environ.get("ANTHROPIC_API_KEY")
        os.environ["ANTHROPIC_API_KEY"] = "sk-ant-test-poison-value-never-used"

        anaxi_final_dir = os.path.dirname(os.path.abspath(__file__))
        repo_root = os.path.dirname(anaxi_final_dir)

        # ---- claude_anaxi.py ----
        sys.modules.pop("claude_anaxi", None)
        import claude_anaxi
        sys.argv = ["claude_anaxi.py", "test prompt"]
        raised = None
        try:
            claude_anaxi.main()
        except Exception as e:
            raised = e
        check("claude_anaxi.py::main() raises ClaudeWakingDeferredError",
              raised is not None and type(raised).__name__ == "ClaudeWakingDeferredError")
        check("claude_anaxi.py::main() wrote zero files anywhere in the isolated CWD",
              os.listdir(test_dir) == [])

        # ---- claude_anaxi_battery.py ----
        sys.modules.pop("claude_anaxi_battery", None)
        import claude_anaxi_battery
        raised = None
        try:
            claude_anaxi_battery.main()
        except Exception as e:
            raised = e
        check("claude_anaxi_battery.py::main() raises ClaudeWakingDeferredError",
              raised is not None and type(raised).__name__ == "ClaudeWakingDeferredError")
        check("claude_anaxi_battery.py::main() wrote zero files anywhere in the isolated CWD",
              os.listdir(test_dir) == [])

        # ---- claude_agent.py (repo root) ----
        if repo_root not in sys.path:
            sys.path.insert(0, repo_root)
        sys.modules.pop("claude_agent", None)
        import claude_agent
        sys.argv = ["claude_agent.py", "test prompt"]
        raised = None
        try:
            claude_agent.main()
        except Exception as e:
            raised = e
        check("claude_agent.py::main() raises ClaudeWakingDeferredError",
              raised is not None and type(raised).__name__ == "ClaudeWakingDeferredError")
        check("claude_agent.py::main() wrote zero files anywhere in the isolated CWD",
              os.listdir(test_dir) == [])
        check("claude_agent.py's guard is its OWN locally-defined exception class, not "
              "imported from anaxi_final/ (A1.3.1: this file is structurally independent "
              "of both anaxi_final/ modules)",
              raised is not None
              and raised.__class__.__module__ == "claude_agent")

        # ---- module import itself remains permitted (A1.3) ----
        check("Module import of all three guarded executables succeeded "
              "(A1.3: 'Module import remains permitted')",
              "claude_anaxi" in sys.modules
              and "claude_anaxi_battery" in sys.modules
              and "claude_agent" in sys.modules)

        # ---- distinct, non-swallowable exception type (A1.3(2)) ----
        check("The raised exception type is distinct -- not a bare Exception/RuntimeError/"
              "ValueError a broad except elsewhere could mistake for something benign",
              raised is not None
              and type(raised) not in (Exception, RuntimeError, ValueError, TypeError))

    finally:
        os.chdir(original_cwd)
        shutil.rmtree(test_dir, ignore_errors=True)
        if original_api_key is None:
            os.environ.pop("ANTHROPIC_API_KEY", None)
        else:
            os.environ["ANTHROPIC_API_KEY"] = original_api_key
        for mod in ("claude_anaxi", "claude_anaxi_battery", "claude_agent", "orchestration", "anthropic"):
            sys.modules.pop(mod, None)

    print(f"\n{'='*70}\n{sum(results)}/{len(results)} pass\n{'='*70}")
    return sum(results) == len(results)


if __name__ == "__main__":
    success = run_test_suite()
    sys.exit(0 if success else 1)
