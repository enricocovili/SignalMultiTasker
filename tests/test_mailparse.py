from email import message_from_string

from bridge.mailparse import decode_mime, extract_sender, strip_quoted_reply


def msg(from_header):
    return message_from_string(f"From: {from_header}\nSubject: x\n\nbody")


def test_decode_mime_rfc2047():
    assert decode_mime("=?utf-8?B?Q2Fmw6g=?=") == "Cafè"
    assert decode_mime("") == ""


def test_extract_sender_name_and_address():
    assert extract_sender(msg("Alice <alice@example.com>")) == (
        "Alice <alice@example.com>",
        "alice@example.com",
    )


def test_extract_sender_bare_address():
    assert extract_sender(msg("bob@example.com")) == ("bob@example.com", "bob@example.com")


def test_extract_sender_encoded_name():
    display, addr = extract_sender(msg("=?utf-8?B?Q2Fmw6g=?= <c@example.com>"))
    assert display == "Cafè <c@example.com>"
    assert addr == "c@example.com"


def test_extract_sender_malformed_has_no_address():
    display, addr = extract_sender(msg("Weird Name Only"))
    assert addr == ""
    assert display == "Weird Name Only"


def test_strip_quoted_reply_at_quote_marker():
    body = "Thanks!\n\n> older text\n> more"
    assert strip_quoted_reply(body) == "Thanks!"


def test_strip_quoted_reply_on_wrote():
    body = "Sure.\n\nOn Mon, 1 Jan 2026, Bob <b@x.com> wrote:\n> hi"
    assert strip_quoted_reply(body) == "Sure."


def test_strip_quoted_reply_italian():
    body = "Ok\n\nIl 1 gen 2026 Bob <b@x.com> ha scritto:\n> ciao"
    assert strip_quoted_reply(body) == "Ok"


def test_strip_quoted_reply_outlook_header_block():
    body = "Reply\n\nFrom: Bob\nSent: Monday\nTo: me"
    assert strip_quoted_reply(body) == "Reply"


def test_strip_quoted_reply_leaves_plain_body():
    assert strip_quoted_reply("just text\n") == "just text"
    assert strip_quoted_reply("") == ""
