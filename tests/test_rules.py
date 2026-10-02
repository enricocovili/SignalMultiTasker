from bridge import rules


def test_matches_google_alert():
    rule = rules.match_rule(
        "no-reply@accounts.google.com",
        "Google <no-reply@accounts.google.com>",
        "Avviso di sicurezza: nuovo accesso",
    )
    assert rule["name"] == "google-security-alert"


def test_all_conditions_must_hold():
    assert rules.match_rule("no-reply@accounts.google.com", "x", "Hello") is None


def test_matching_is_case_insensitive():
    rule = rules.match_rule(
        "NOREPLY@NOTIFY.CLOUDFLARE.COM", "x", "your cloudflare login token"
    )
    assert rule["name"] == "cloudflare-login-token"


def test_unknown_condition_fails_closed(monkeypatch):
    monkeypatch.setattr(
        rules, "RULES", [{"name": "typo", "match": {"sender_cotains": "a"}, "message": "m"}]
    )
    assert rules.match_rule("a@b.c", "a", "s") is None
