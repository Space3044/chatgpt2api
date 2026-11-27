package main

import (
	"math"
	"testing"
)

// silenceFrames returns n 40ms frames of digital silence.
func silenceFrames(n int) []byte {
	return make([]byte, n*silenceFrameBytes)
}

// speechFrames returns n 40ms frames of a loud sine wave (peak 8000,
// RMS ~5650), well above the silence thresholds.
func speechFrames(n int) []byte {
	out := make([]byte, n*silenceFrameBytes)
	for i := 0; i < n*silenceFrameSamples; i++ {
		v := int16(8000 * math.Sin(2*math.Pi*440*float64(i)/16000))
		out[2*i] = byte(v)
		out[2*i+1] = byte(v >> 8)
	}
	return out
}

// quietSpeechFrames returns n frames of a quiet sine wave (peak 400,
// RMS ~283): peak is under the silence peak threshold but RMS is
// over it, so these frames must NOT count as silence.
func quietSpeechFrames(n int) []byte {
	out := make([]byte, n*silenceFrameBytes)
	for i := 0; i < n*silenceFrameSamples; i++ {
		v := int16(400 * math.Sin(2*math.Pi*440*float64(i)/16000))
		out[2*i] = byte(v)
		out[2*i+1] = byte(v >> 8)
	}
	return out
}

func framesOf(pcm []byte) int { return len(pcm) / silenceFrameBytes }

func TestJoinChunkText(t *testing.T) {
	if got := joinChunkText("你好", "世界"); got != "你好世界" {
		t.Fatalf("chinese join: got %q", got)
	}
	if got := joinChunkText("Hello", "world"); got != "Hello world" {
		t.Fatalf("english join: got %q", got)
	}
	if got := joinChunkText("Hello.", "World"); got != "Hello.World" {
		t.Fatalf("punctuation join: got %q", got)
	}
	if got := joinChunkText("", "abc"); got != "abc" {
		t.Fatalf("empty acc join: got %q", got)
	}
}

func TestLiveSilenceFilterShortRunKept(t *testing.T) {
	// The live filter cannot know how long a run will be, so it
	// keeps the first ~0.5s of every silence run and drops the
	// rest: a 1s run is cut to the keep window, which still leaves
	// a clear word boundary for the server's VAD.
	f := &liveSilenceFilter{}
	kept := 0
	for i := 0; i < 25; i++ {
		if f.feed(silenceFrames(1)) {
			kept++
		}
	}
	if kept != silenceKeepFrames {
		t.Fatalf("short silence run: kept %d frames, want %d", kept, silenceKeepFrames)
	}
}

func TestLiveSilenceFilterLongRunCut(t *testing.T) {
	// 10s of silence: only the first ~0.5s reaches the session.
	f := &liveSilenceFilter{}
	kept := 0
	for i := 0; i < 250; i++ {
		if f.feed(silenceFrames(1)) {
			kept++
		}
	}
	if kept != silenceKeepFrames {
		t.Fatalf("long silence run: kept %d frames, want %d", kept, silenceKeepFrames)
	}
}

func TestLiveSilenceFilterSpeechResets(t *testing.T) {
	// Speech resets the run, so a second silence run is kept
	// again from its first frames; speech frames always pass.
	f := &liveSilenceFilter{}
	for i := 0; i < silenceKeepFrames+10; i++ {
		f.feed(silenceFrames(1))
	}
	if !f.feed(speechFrames(1)) {
		t.Fatalf("speech frame after silence was dropped")
	}
	kept := 0
	for i := 0; i < silenceKeepFrames; i++ {
		if f.feed(silenceFrames(1)) {
			kept++
		}
	}
	if kept != silenceKeepFrames {
		t.Fatalf("second silence run: kept %d frames, want %d", kept, silenceKeepFrames)
	}
}

func TestLiveSilenceFilterQuietSpeechKept(t *testing.T) {
	// Quiet speech (peak under the silence peak threshold but RMS
	// over it) must never count as silence.
	f := &liveSilenceFilter{}
	for i := 0; i < 100; i++ {
		if !f.feed(quietSpeechFrames(1)) {
			t.Fatalf("quiet speech frame %d was dropped", i)
		}
	}
}

func TestStripConfirmedPrefix(t *testing.T) {
	// Interim is the cumulative text: only the new tail is shown.
	if got := stripConfirmedPrefix("今天天气很好", "今天天气很好，我们一起去公园散步吧。"); got != "，我们一起去公园散步吧。" {
		t.Fatalf("extension: got %q", got)
	}
	// Nothing confirmed yet: the whole interim is shown.
	if got := stripConfirmedPrefix("", "你好世界"); got != "你好世界" {
		t.Fatalf("empty confirmed: got %q", got)
	}
	// Interim has not grown past the confirmed text: nothing new.
	if got := stripConfirmedPrefix("今天天气很好", "今天天气很好"); got != "" {
		t.Fatalf("equal: got %q", got)
	}
	// A revision in the cumulative text shortens the shared prefix;
	// the rest is shown greyed until it settles.
	if got := stripConfirmedPrefix("充一次电能用用两天", "充一次电能用两天，老师"); got != "两天，老师" {
		t.Fatalf("revision: got %q", got)
	}
	// A new session's text shares nothing with the old confirmed text.
	if got := stripConfirmedPrefix("今天天气很好", "晚上我们一起吃火锅"); got != "晚上我们一起吃火锅" {
		t.Fatalf("unrelated: got %q", got)
	}
}
