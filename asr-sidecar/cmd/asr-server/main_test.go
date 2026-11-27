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

func TestStripShortSilenceUntouched(t *testing.T) {
	// 1s of silence between speech: under the cut threshold, kept whole.
	in := append(append(speechFrames(25), silenceFrames(25)...), speechFrames(25)...)
	out := stripLongSilences(in)
	if len(out) != len(in) {
		t.Fatalf("short silence changed length: got %d frames, want %d", framesOf(out), framesOf(in))
	}
}

func TestStripLongSilenceInMiddle(t *testing.T) {
	// 5s of silence between speech: cut to ~0.5s, speech intact.
	in := append(append(speechFrames(25), silenceFrames(125)...), speechFrames(25)...)
	out := stripLongSilences(in)
	want := 25 + silenceKeepFrames + 25
	if framesOf(out) != want {
		t.Fatalf("long silence: got %d frames, want %d", framesOf(out), want)
	}
	// Speech content is preserved verbatim on both sides.
	for i := 0; i < 25*silenceFrameBytes; i++ {
		if out[i] != in[i] {
			t.Fatalf("leading speech changed at byte %d", i)
		}
	}
	tail := out[len(out)-25*silenceFrameBytes:]
	wantTail := in[len(in)-25*silenceFrameBytes:]
	for i := range tail {
		if tail[i] != wantTail[i] {
			t.Fatalf("trailing speech changed at byte %d", i)
		}
	}
}

func TestStripLeadingAndAllSilence(t *testing.T) {
	// 10s leading silence is cut to ~0.5s; all-silence input shrinks the same way.
	in := append(silenceFrames(250), speechFrames(25)...)
	if got := framesOf(stripLongSilences(in)); got != silenceKeepFrames+25 {
		t.Fatalf("leading silence: got %d frames, want %d", got, silenceKeepFrames+25)
	}
	if got := framesOf(stripLongSilences(silenceFrames(250))); got != silenceKeepFrames {
		t.Fatalf("all silence: got %d frames, want %d", got, silenceKeepFrames)
	}
}

func TestStripQuietSpeechKept(t *testing.T) {
	// Quiet speech (peak under threshold, RMS over it) is not silence:
	// a 5s run of it must survive untouched.
	in := quietSpeechFrames(125)
	if got := len(stripLongSilences(in)); got != len(in) {
		t.Fatalf("quiet speech stripped: got %d bytes, want %d", got, len(in))
	}
}

func TestStripPartialTrailingFrame(t *testing.T) {
	in := append(speechFrames(25), 0x01, 0x02, 0x03)
	out := stripLongSilences(in)
	if len(out) != len(in) || out[len(out)-3] != 0x01 || out[len(out)-1] != 0x03 {
		t.Fatalf("trailing partial frame not preserved: got %d bytes, want %d", len(out), len(in))
	}
}

func TestSplitShortUntouched(t *testing.T) {
	// Under the target length, one chunk, content unchanged.
	in := speechFrames(150)
	chunks := splitIntoChunks(in)
	if len(chunks) != 1 || len(chunks[0]) != len(in) {
		t.Fatalf("short input split into %d chunks", len(chunks))
	}
}

func TestSplitCutsAtSilence(t *testing.T) {
	// Speech, then a 0.5s silence at ~8s, then more speech: the cut
	// must land at the silence, not mid-word, and no chunk may
	// exceed the hard max.
	in := append(append(speechFrames(200), silenceFrames(12)...), speechFrames(200)...)
	chunks := splitIntoChunks(in)
	if len(chunks) != 2 {
		t.Fatalf("got %d chunks, want 2", len(chunks))
	}
	if framesOf(chunks[0]) != 200 {
		t.Fatalf("first chunk is %d frames, want 200 (cut at silence)", framesOf(chunks[0]))
	}
	if framesOf(chunks[1]) != 212 {
		t.Fatalf("second chunk is %d frames, want 212", framesOf(chunks[1]))
	}
}

func TestSplitUsesWideWindow(t *testing.T) {
	// Silence at ~7s, then dense speech with no silence until past
	// the hard max: the cut must use the earlier silence instead
	// of hard-cutting mid-word at 10s.
	in := append(append(speechFrames(175), silenceFrames(10)...), speechFrames(300)...)
	chunks := splitIntoChunks(in)
	if len(chunks) != 3 {
		t.Fatalf("got %d chunks, want 3", len(chunks))
	}
	if framesOf(chunks[0]) != 175 {
		t.Fatalf("first chunk is %d frames, want 175 (cut at silence)", framesOf(chunks[0]))
	}
}

func TestSplitHardCutWithoutSilence(t *testing.T) {
	// No silence at all: hard cuts every ~10s, nothing lost.
	in := speechFrames(600)
	chunks := splitIntoChunks(in)
	if len(chunks) != 3 {
		t.Fatalf("got %d chunks, want 3", len(chunks))
	}
	for i, want := range []int{250, 250, 100} {
		if framesOf(chunks[i]) != want {
			t.Fatalf("chunk %d is %d frames, want %d", i, framesOf(chunks[i]), want)
		}
	}
	total := 0
	for _, c := range chunks {
		total += len(c)
	}
	if total != len(in) {
		t.Fatalf("split lost bytes: got %d, want %d", total, len(in))
	}
}

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
