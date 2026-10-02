import base64

from bridge import config, voice


def test_group_recipient_is_rewrapped():
    data = {"groupInfo": {"groupId": "rawid="}}
    expected = "group." + base64.b64encode(b"rawid=").decode()
    assert voice.conversation_recipient({}, data) == expected


def test_one_to_one_uses_source():
    assert voice.conversation_recipient({"sourceNumber": "+391"}, {}) == "+391"


def test_sync_message_uses_destination():
    env = {"sourceNumber": "+39me"}
    assert voice.conversation_recipient(env, {"destinationNumber": "+39other"}) == "+39other"


def test_fallback_to_signal_group(monkeypatch):
    monkeypatch.setattr(config, "SIGNAL_GROUP_ID", "group.default")
    assert voice.conversation_recipient({}, {}) == "group.default"


def test_whitelist(monkeypatch):
    monkeypatch.setattr(config, "VOICE_ALLOWED_CHATS", {"group.ok", "+391"})
    assert voice.is_allowed_chat("group.ok")
    assert voice.is_allowed_chat("+391")
    assert not voice.is_allowed_chat("group.other")


def test_empty_whitelist_allows_nothing(monkeypatch):
    monkeypatch.setattr(config, "VOICE_ALLOWED_CHATS", set())
    assert not voice.is_allowed_chat("group.any")


def test_iter_voice_messages_handles_data_and_sync():
    audio = {"attachments": [{"contentType": "audio/aac", "id": "1"}]}
    text = {"attachments": [{"contentType": "image/png", "id": "2"}]}
    envs = [
        {"envelope": {"dataMessage": audio}},
        {"envelope": {"syncMessage": {"sentMessage": audio}}},
        {"envelope": {"dataMessage": text}},
        {"envelope": {}},
        "garbage",
    ]
    assert len(list(voice.iter_voice_messages(envs))) == 2
