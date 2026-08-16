from app.data.backends import normalize_backend, validate_backend, validate_source_name


def test_empty_backend_ok():
    assert validate_backend("") is None
    assert validate_backend("  ") is None
    assert normalize_backend("  host:1  ") == "host:1"


def test_valid_backends():
    assert validate_backend("192.168.1.10:8080") is None
    assert validate_backend("localai:8080") is None
    assert validate_backend("ollama") is None


def test_invalid_backends():
    assert validate_backend("bad host") is not None
    assert validate_backend("host:99999") is not None
    assert validate_backend("http://x:1") is not None


def test_source_names():
    assert validate_source_name("ollama") is None
    assert validate_source_name("gpu-2") is None
    assert validate_source_name("") is not None
    assert validate_source_name("s") is not None
    assert validate_source_name("2bad") is not None
