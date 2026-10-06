"""Exercise loopback HTTP fault acceptance against the installed wheel only."""
import importlib.metadata
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import unittest

import contractdock


def main():
    repository = Path(__file__).resolve().parents[1]
    installed = Path(contractdock.__file__).resolve()
    if (installed.is_relative_to(repository) and not installed.is_relative_to(repository / '.venv')) or "site-packages" not in installed.parts:
        raise RuntimeError("Expected installed wheel, not repository source")
    version = importlib.metadata.version("contractdock")
    if version != "0.4.0" or contractdock.__version__ != version:
        raise RuntimeError("Expected aligned ContractDock 0.4.0")
    spec = importlib.util.spec_from_file_location(
        "installed_http_faults", repository / "tests" / "test_http_faults.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    result = unittest.TextTestRunner(verbosity=2).run(
        unittest.defaultTestLoader.loadTestsFromModule(module))
    if not result.wasSuccessful() or result.testsRun != 9:
        return 1
    spec = importlib.util.spec_from_file_location('installed_openapi', repository / 'tests' / 'test_openapi.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    acceptance = unittest.TextTestRunner(verbosity=2).run(unittest.defaultTestLoader.loadTestsFromModule(module))
    if not acceptance.wasSuccessful() or acceptance.testsRun != 22:
        return 1
    demo = subprocess.run([sys.executable, '-I', str(repository / 'examples' / 'openapi_response.py')], capture_output=True, text=True)
    if demo.returncode != 0 or not json.loads(demo.stdout)['offline']:
        raise RuntimeError('Installed OpenAPI demo failed')
    command = subprocess.run([sys.executable, "-I", "-m", "contractdock", "--help"],
                             capture_output=True, text=True)
    if command.returncode != 0 or "scenario" not in command.stdout or 'openapi-check' not in command.stdout:
        raise RuntimeError("Installed CLI smoke test failed")
    print(json.dumps({"version": version, "fault_tests": result.testsRun, 'openapi_tests': acceptance.testsRun,
                      "installed": str(installed), "cli": "passed"}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
