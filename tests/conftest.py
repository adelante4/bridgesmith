import os
import tempfile

# Must be set before app.db is imported anywhere, since it reads DATA_DIR at import time.
os.environ.setdefault("DATA_DIR", tempfile.mkdtemp(prefix="marketing_test_"))
os.environ.setdefault("ANTHROPIC_API_KEY", "sk-test-dummy")
