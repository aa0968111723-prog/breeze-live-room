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
