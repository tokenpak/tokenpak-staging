from tokenpak.proxy.spend_guard import classifier as c


def test_precedence_and_classes():
    assert c.classify({"X-Tokenpak-Agent": "Trix"}) == c.Classification(c.MANAGED, c.HEADER_AGENT, "trix")
    assert c.classify({"X-Tokenpak-Managed": "true"}).request_class == c.MANAGED
    assert c.classify({"X-Tokenpak-Managed-Env": "1"}).reason == c.ENV_LAUNCHER
    assert c.classify({"User-Agent": "claude-cli/1"}).request_class == c.RAW_CLAUDE_OBSERVED
    assert c.classify({"User-Agent": "curl"}).request_class == c.EXTERNAL_UNTAGGED


def test_higher_precedence_wins_and_false_marker_is_external():
    result = c.classify({"X-Tokenpak-Agent": "Sue", "X-Tokenpak-Managed": "1", "User-Agent": "claude-cli"})
    assert result.reason == c.HEADER_AGENT
    assert c.classify({"X-Tokenpak-Managed": "0"}).request_class == c.EXTERNAL_UNTAGGED


def test_case_insensitive_and_read_only():
    headers = {"x-tokenpak-agent": "Cali", "User-Agent": "claude-cli"}
    before = dict(headers)
    assert c.classify(headers).agent_attribution == "cali"
    assert headers == before


def test_strip_internal_headers_only():
    headers = {"X-Tokenpak-Agent": "Trix", "x-tokenpak-managed": "1", "Authorization": "x"}
    removed = c.strip_managed_headers(headers)
    assert headers == {"Authorization": "x"}
    assert set(removed) == {"X-Tokenpak-Agent", "x-tokenpak-managed"}


def test_empty_headers_and_noop_strip():
    assert c.classify(None).reason == c.NO_MARKER
    headers = {"Authorization": "x"}
    assert c.strip_managed_headers(headers) == []
