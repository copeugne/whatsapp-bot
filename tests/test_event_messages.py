from pathlib import Path

from whatsapp_bot.event_messages import EventMessageMap


def test_tracks_persists_and_removes_event_message(tmp_path: Path) -> None:
    path = tmp_path / "event_messages.json"
    event_messages = EventMessageMap(str(path))
    uid = event_messages.new_uid()

    assert len(uid) == 6
    assert uid.isalnum()

    event_messages.track(uid, 42, "message-1", "group@g.us")
    reloaded = EventMessageMap(str(path))
    assert reloaded.get(uid.lower()).message_id == "message-1"
    assert reloaded.get_by_message_id("message-1").event_id == 42

    reloaded.remove(uid)
    assert EventMessageMap(str(path)).get(uid) is None
