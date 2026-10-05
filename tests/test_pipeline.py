from app.pipeline import RoomPipeline

def test_duplicate_seq_does_not_create_second_event():
    pipe = RoomPipeline()
    first, created = pipe.accept("class", "s1", 1, "般若")
    again, created_again = pipe.accept("class", "s1", 1, "般若")
    assert created is True
    assert created_again is False
    assert again is first

def test_translation_failure_keeps_chinese():
    pipe = RoomPipeline()
    event, _ = pipe.accept("class", "s1", 2, "空性", translate_error="timeout")
    assert event["zh"] == "空性"
    assert event["translate_status"] == "error"

def test_order_follows_seq_not_arrival():
    pipe = RoomPipeline()
    pipe.accept("class", "s1", 2, "第二")
    pipe.accept("class", "s1", 1, "第一")
    assert [row["zh"] for row in pipe.ordered("s1")] == ["第一", "第二"]
