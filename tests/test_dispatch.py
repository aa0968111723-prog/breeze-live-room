from app.dispatch import RoomBus

def test_rooms_do_not_mix():
    bus = RoomBus()
    bus.publish({"id": "a:s:1", "room_id": "a", "seq": 1, "version": 1, "zh": "般若"})
    bus.publish({"id": "b:s:1", "room_id": "b", "seq": 1, "version": 1, "zh": "空性"})
    assert [item["zh"] for item in bus.history("a")] == ["般若"]
    assert [item["zh"] for item in bus.history("b")] == ["空性"]

def test_same_segment_updates_in_place():
    bus = RoomBus()
    bus.publish({"id": "a:s:1", "room_id": "a", "version": 1, "zh": "般若", "en": ""})
    bus.publish({"id": "a:s:1", "room_id": "a", "version": 2, "zh": "般若", "en": "prajna"})
    assert len(bus.history("a")) == 1
    assert bus.history("a")[0]["en"] == "prajna"


def test_stale_version_does_not_take_a_cursor():
    bus = RoomBus()
    first = bus.publish({"id": "a:s:1", "room_id": "a", "session_ord": 1, "seq": 1, "version": 2, "zh": "新"})
    stale = bus.publish({"id": "a:s:1", "room_id": "a", "session_ord": 1, "seq": 1, "version": 1, "zh": "舊"})
    assert stale is None
    assert bus.latest_cursor("a") == first["cursor"]
    assert bus.history("a")[0]["zh"] == "新"


def test_drop_forgets_version_high_water():
    bus = RoomBus()
    bus.publish({"id": "a:s:1", "room_id": "class", "session_id": "s", "session_ord": 1, "seq": 1, "version": 3, "zh": "舊"})
    bus.drop("class")
    again = bus.publish({"id": "a:s:1", "room_id": "class", "session_id": "s", "session_ord": 1, "seq": 1, "version": 1, "zh": "新開"})
    assert again is not None
    assert again["zh"] == "新開"
    assert bus.history("class")[0]["zh"] == "新開"
    again["zh"] = "被改"
    assert bus.history("class")[0]["zh"] == "新開"
