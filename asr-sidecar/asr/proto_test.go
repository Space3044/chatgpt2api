package asr

import "testing"

func TestMarshalParseRequestRoundTrip(t *testing.T) {
	data := marshalRequest("OrnqKvSSrs", "StartTask", "", nil, "3b115ac9-c064-464d-a2c3-831dcada4150", 0)
	// Captured StartTask frame is exactly 66 bytes.
	if len(data) != 66 {
		t.Fatalf("StartTask frame: expected 66 bytes, got %d", len(data))
	}
	// Field 2 (token) must be the first field, as in the capture.
	if data[0] != 0x12 || data[1] != 10 || string(data[2:12]) != "OrnqKvSSrs" {
		t.Fatalf("token field mismatch: %x", data[:12])
	}
}

func TestParseResponseTaskStarted(t *testing.T) {
	// Hand-built TaskStarted response, same shape as the live one.
	var b []byte
	b = appendStringField(b, 1, "req-1")
	b = appendStringField(b, 2, "task-1")
	b = appendStringField(b, 3, "ASR")
	b = appendStringField(b, 4, "TaskStarted")
	b = appendVarintField(b, 5, 20000000)
	b = appendStringField(b, 6, "OK")
	resp, err := parseResponse(b)
	if err != nil {
		t.Fatal(err)
	}
	if resp.MessageType != "TaskStarted" || resp.StatusCode != 20000000 || resp.TaskID != "task-1" {
		t.Fatalf("unexpected response: %+v", resp)
	}
}

func TestParseResultJSON(t *testing.T) {
	s := `{"results":[{"text":"你好，这","is_interim":true},{"text":"你好这","is_interim":true}],"extra":{"seq_id":25}}`
	rj, err := parseResultJSON(s)
	if err != nil {
		t.Fatal(err)
	}
	if rj.text() != "你好，这" {
		t.Fatalf("expected formatted text from results[0], got %q", rj.text())
	}
	if rj.isFinal() {
		t.Fatal("interim payload must not be final")
	}
	fin := `{"results":[{"text":"你好。","is_interim":false,"is_vad_finished":true},{"text":"你好。","is_interim":false,"extra":{"nonstream_result":true}}],"extra":{}}`
	rj, err = parseResultJSON(fin)
	if err != nil {
		t.Fatal(err)
	}
	if !rj.isFinal() || rj.finalText() != "你好。" {
		t.Fatalf("final parse failed: %+v", rj)
	}
}
