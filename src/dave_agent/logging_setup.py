"""Logging with secret redaction for API keys and bearer tokens."""

import logging
import os
import re

SECRET_ENV_VARS = ("OPENROUTER_API_KEY", "AZURE_OPENAI_API_KEY")
_BEARER = re.compile(r"(Bearer\s+)[A-Za-z0-9._\-]+", re.IGNORECASE)
_KEYLIKE = re.compile(r"\bsk-[A-Za-z0-9_\-]{8,}")
REDACTED = "[REDACTED]"


def redact(text: str) -> str:
    for name in SECRET_ENV_VARS:
        value = os.environ.get(name)
        if value and len(value) >= 8:
            text = text.replace(value, REDACTED)
    text = _BEARER.sub(rf"\1{REDACTED}", text)
    return _KEYLIKE.sub(REDACTED, text)


class RedactingFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = redact(record.getMessage())
        record.args = ()
        return True


def configure_logging(level: str = "INFO") -> None:
    handler = logging.StreamHandler()
    handler.addFilter(RedactingFilter())
    handler.setFormatter(logging.Formatter("%(levelname)s %(name)s: %(message)s"))
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level)
