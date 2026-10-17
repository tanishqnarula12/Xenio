import logging

from app.logging_setup import Redactor, StructuredFormatter, event, setup_logging


def test_redactor_masks_known_secrets_and_patterns():
    r = Redactor(["my-super-secret-value"])
    text = r(
        "key my-super-secret-value; Authorization: Bearer abcdefghijklmnop; nbh_0123456789abcdef0123; "
        "sk-ABCDEFGHIJKLMNOPQRST; api_key=hunter2hunter2 password: p4ssw0rd"
    )
    for leaked in ("my-super-secret-value", "abcdefghijklmnop", "nbh_0123456789abcdef0123", "sk-ABCDEFGH", "hunter2", "p4ssw0rd"):
        assert leaked not in text
    assert "Bearer ***" in text


def test_structured_fields_rendered_and_redacted():
    formatter = StructuredFormatter(Redactor(["topsecret123"]))
    record = logging.LogRecord("xenio.test", logging.INFO, __file__, 1, "TTS started", None, None)
    record.fields = {"chars": 12, "note": "has topsecret123 inside"}
    line = formatter.format(record)
    assert "TTS started chars=12" in line
    assert "topsecret123" not in line


def test_log_file_never_contains_secret(tmp_path):
    secret = "nbh_realkey_abcdef0123456789"
    log_file = setup_logging(tmp_path, [secret], "DEBUG")
    log = logging.getLogger("xenio.test")
    event(log, "STT request started", url="https://x", header=f"Bearer {secret}")
    try:
        raise RuntimeError(f"upstream said: invalid key {secret}")
    except RuntimeError:
        log.exception("STT failed")
    for handler in logging.getLogger("xenio").handlers:
        handler.flush()
    content = log_file.read_text(encoding="utf-8")
    assert "STT request started" in content
    assert "STT failed" in content
    assert secret not in content
