package asr

import "testing"

func TestOpusSilenceFrameSize(t *testing.T) {
	enc, err := newOpusEncoder(16000, 1)
	if err != nil {
		t.Fatal(err)
	}
	defer enc.Close()
	// Silence, 640 samples (40ms) at CBR 64kbps must be exactly 320 bytes —
	// the same size the Rust addon sends per frame (verified in the capture).
	out, err := enc.Encode(make([]byte, 1280))
	if err != nil {
		t.Fatal(err)
	}
	if len(out) != 320 {
		t.Fatalf("expected 320 bytes, got %d", len(out))
	}
}

func TestOpusRejectsShortFrame(t *testing.T) {
	enc, err := newOpusEncoder(16000, 1)
	if err != nil {
		t.Fatal(err)
	}
	defer enc.Close()
	if _, err := enc.Encode(make([]byte, 640)); err == nil {
		t.Fatal("expected error for a 320-sample frame")
	}
}
