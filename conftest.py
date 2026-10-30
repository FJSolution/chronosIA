"""Never run API tests against the user's databases or provider credentials."""
import os
import tempfile

os.environ["CHRONOS_DATA_DIR"] = tempfile.mkdtemp(prefix="chronos-tests-")
os.environ["CHRONOS_DEMO_MODE"] = "1"
os.environ["CHRONOS_API_TOKEN"] = "test-only-token-" * 4
os.environ.pop("CHRONOS_TRACE_DB", None)
os.environ.pop("CHRONOS_LEDGER_DB", None)
os.environ.pop("CHRONOS_READ_TOKEN", None)
os.environ.pop("GEMINI_API_KEY", None)
os.environ.pop("OPENAI_API_KEY", None)
