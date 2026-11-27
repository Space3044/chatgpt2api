// Command asr-server is the cometix-asr sidecar for chat2api.
//
// It runs on loopback only (default 127.0.0.1:3101, override with
// ASR_SIDECAR_ADDR) and is only ever called by the chat2api Python
// backend in the same container; the browser never talks to it
// directly, and it is never exposed on a public port.
//
// Endpoints (all JSON unless noted):
//
//	GET  /healthz    {"ok":true} — no auth, used by the Python backend
//	WS   /ws         browser-relay protocol: {"type":"start"} / binary
//	                  16kHz mono s16le PCM frames / {"type":"stop"};
//	                  server replies ready/transcript/final/error/
//	                  close JSON. Transcript events carry a
//	                  "confirmed" field (all confirmed text so far)
//	                  alongside the live interim text; {"type":"final"}
//	                  carries the whole recording's text on stop.
//	                  Long silences are compressed live and a
//	                  Doubao session that ends mid-recording is
//	                  restarted transparently, so one recording
//	                  survives pauses (see wsStream).
//	POST /postasr    {"text","enable_fmt","enable_ner","sami_token","uid"}
//	                  (did is filled from the cached device id when
//	                  empty) — returns the RunPostAsr result JSON.
//
// Auth: if ASR_SIDECAR_KEY is set, every endpoint except /healthz
// requires header X-Asr-Key with that value. The Python backend reads
// the same variable from the container environment, so the key never
// appears in code or in the repo. If it is unset, loopback-only
// binding is the only protection.
//
// Mock mode is not ported (see asr/asr.go): only the live ASR path
// and the standalone RunPostAsr are served here.
//
// Transcripts are never logged: logs carry only lengths, counts and
// status, never recognised text or tokens.
package main

import (
	"encoding/json"
	"io"
	"log"
	"net/http"
	"os"
	"strings"
	"sync"
	"time"

	"github.com/gorilla/websocket"

	"cometix-asr-go/asr"
)

// Silence stripping for the live session. The Doubao IME server ends
// a session after a long silence (verified live: a 10s pause in the
// middle of an utterance drops everything after it, while 1s/3s/5s
// pauses are fine), so a user who pauses to think loses the second
// half of what they say — including leading silence while they think
// after clicking the mic. The live filter (liveSilenceFilter) keeps
// only the first ~0.5s of each silence run and drops the rest before
// the audio reaches the server.
//
// A 40ms frame counts as silent only when BOTH its peak amplitude and
// its RMS are below conservative thresholds, so quiet speech is never
// stripped. 0.5s of each silence run is kept so word boundaries and
// natural pacing survive. Only full 40ms frames are examined; a
// trailing partial frame is passed through untouched.
const (
	silenceFrameSamples = 640 // 40ms at 16kHz
	silenceFrameBytes   = silenceFrameSamples * 2
	silenceKeepFrames   = 12 // keep ~0.5s of a cut run
	silencePeakMax      = 500
	silenceRMSMax       = 200
)

func frameIsSilent(frame []byte) bool {
	var peak int
	var sumSq int64
	for i := 0; i+1 < len(frame); i += 2 {
		v := int(int16(frame[i]) | int16(frame[i+1])<<8)
		if v < 0 {
			v = -v
		}
		if v > peak {
			peak = v
		}
		s := int64(int16(frame[i]) | int16(frame[i+1])<<8)
		sumSq += s * s
	}
	if peak >= silencePeakMax {
		return false
	}
	n := len(frame) / 2
	if n == 0 {
		return true
	}
	// RMS via integer square root approximation: compare sumSq/n
	// against threshold^2 without floating point.
	return sumSq/int64(n) < int64(silenceRMSMax)*int64(silenceRMSMax)
}

var upgrader = websocket.Upgrader{
	ReadBufferSize:  64 * 1024,
	WriteBufferSize: 64 * 1024,
	// Loopback-only server behind the Python backend; the browser
	// never connects here directly, so origin is not a trust boundary.
	CheckOrigin: func(r *http.Request) bool { return true },
}

func sidecarKey() string { return os.Getenv("ASR_SIDECAR_KEY") }

func requireKey(next http.HandlerFunc) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		if key := sidecarKey(); key != "" && r.Header.Get("X-Asr-Key") != key {
			http.Error(w, `{"error":"unauthorized"}`, http.StatusUnauthorized)
			return
		}
		next(w, r)
	}
}

func writeJSON(w http.ResponseWriter, status int, v any) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	_ = json.NewEncoder(w).Encode(v)
}

func writeErr(w http.ResponseWriter, status int, msg string) {
	writeJSON(w, status, map[string]string{"error": msg})
}

// joinChunkText joins per-chunk finals. Chinese needs no separator;
// between two alphanumerics (e.g. English words split across chunks)
// a single space is inserted, matching the composer's insertion rule.
func joinChunkText(acc, next string) string {
	if acc == "" {
		return next
	}
	if next == "" {
		return acc
	}
	ar := []rune(acc)
	nr := []rune(next)
	if isAlnumRune(ar[len(ar)-1]) && isAlnumRune(nr[0]) {
		return acc + " " + next
	}
	return acc + next
}

func isAlnumRune(r rune) bool {
	return r >= '0' && r <= '9' || r >= 'a' && r <= 'z' || r >= 'A' && r <= 'Z'
}

// stripConfirmedPrefix removes the longest common rune prefix of
// confirmed and text: the interim text is cumulative, so its head is
// the confirmed text (modulo a revision, which shortens the shared
// prefix — the rest is shown greyed until it settles).
func stripConfirmedPrefix(confirmed, text string) string {
	cr, tr := []rune(confirmed), []rune(text)
	n := 0
	for n < len(cr) && n < len(tr) && cr[n] == tr[n] {
		n++
	}
	return string(tr[n:])
}

func handleHealthz(w http.ResponseWriter, r *http.Request) {
	writeJSON(w, http.StatusOK, map[string]bool{"ok": true})
}

func handlePostAsr(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost {
		writeErr(w, http.StatusMethodNotAllowed, "method not allowed")
		return
	}
	var body struct {
		Text      string  `json:"text"`
		EnableFmt *bool   `json:"enable_fmt"`
		EnableNer *bool   `json:"enable_ner"`
		SamiToken *string `json:"sami_token"`
		UID       string  `json:"uid"`
		Did       string  `json:"did"`
	}
	if err := json.NewDecoder(io.LimitReader(r.Body, 64*1024)).Decode(&body); err != nil {
		writeErr(w, http.StatusBadRequest, "bad json: "+err.Error())
		return
	}
	if strings.TrimSpace(body.Text) == "" {
		writeErr(w, http.StatusBadRequest, "text is required")
		return
	}
	did := body.Did
	if did == "" {
		did = asr.GetCachedDid()
	}
	if did == "" {
		if d, err := asr.EnsureDid(false); err == nil {
			did = d
		}
	}
	cfgMap := map[string]any{"did": did, "uid": body.UID}
	if body.EnableFmt != nil {
		cfgMap["enableFmt"] = *body.EnableFmt
	}
	if body.EnableNer != nil {
		cfgMap["enableNer"] = *body.EnableNer
	}
	if body.SamiToken != nil {
		cfgMap["samiToken"] = *body.SamiToken
	}
	cfgJSON, _ := json.Marshal(cfgMap)
	out, err := asr.RunPostAsr(string(cfgJSON), body.Text)
	if err != nil {
		writeErr(w, http.StatusBadRequest, err.Error())
		return
	}
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(http.StatusOK)
	_, _ = w.Write([]byte(out))
}

// liveSilenceFilter compresses long silence runs as frames arrive:
// the Doubao IME server ends a session after a long silence, and in
// a live stream the future audio is not known yet, so only the first
// ~0.5s of each silence run is kept and the rest is dropped. Short
// pauses are
// unaffected in practice: a run under ~0.5s passes through whole,
// and a longer run still leaves a clear word boundary.
type liveSilenceFilter struct {
	silentRun int // consecutive silent frames in the current run
}

// feed reports whether a 40ms frame should reach the live session.
func (f *liveSilenceFilter) feed(frame []byte) bool {
	if !frameIsSilent(frame) {
		f.silentRun = 0
		return true
	}
	f.silentRun++
	return f.silentRun <= silenceKeepFrames
}

// maxStreamRestarts bounds transparent session restarts on one
// streaming connection; beyond that the peer gets an error and the
// recording fails with a clear message.
const maxStreamRestarts = 10

// restartGrace is how long a finished session's trailing stable /
// session_final events get to arrive (they are emitted from
// separate goroutines, so the close event can overtake them) before
// the finished text is folded into the connection total and a new
// session is started.
const restartGrace = 300 * time.Millisecond

// wsStream is one browser-relay connection: one long recording
// streamed through (possibly several) short Doubao live sessions.
// The Go session ends on its own after a long silence or ~85s of
// wall time; when that happens mid-recording a new session is
// started and the finals are joined, so the peer sees one
// continuous transcript. Transcripts are never logged.
type wsStream struct {
	conn *websocket.Conn

	sendMu sync.Mutex // serialises writes to the peer

	mu          sync.Mutex
	handle      int  // current Doubao session, -1 when none
	ready       bool // current session completed its handshake
	gen         int  // session generation; stale callbacks ignore events
	stopped     bool // peer sent {"type":"stop"}
	peerGone    bool
	starting    bool          // a StartSession is in flight
	startDone   chan struct{} // closed when the in-flight start settles
	restartPend bool          // a delayed restart is scheduled
	restarts    int
	confirmed   string // finals from finished sessions
	stable      string // latest StableText of the current session
	queued      [][]byte
	partial     []byte // bytes of an incomplete 40ms frame
	filter      liveSilenceFilter
	finalSent   bool
}

func newWSStream(conn *websocket.Conn) *wsStream {
	return &wsStream{conn: conn, handle: -1}
}

func (s *wsStream) send(v any) {
	s.sendMu.Lock()
	defer s.sendMu.Unlock()
	_ = s.conn.WriteJSON(v)
}

// currentConfirmedLocked joins finished-session finals with the
// current session's latest stable text. Caller must hold s.mu.
func (s *wsStream) currentConfirmedLocked() string {
	return joinChunkText(s.confirmed, s.stable)
}

// absorbLocked folds the current session's stable text into the
// finished-session total. Caller must hold s.mu.
func (s *wsStream) absorbLocked() {
	s.confirmed = joinChunkText(s.confirmed, s.stable)
	s.stable = ""
}

// drain feeds queued frames to the current session while one is
// ready. It releases s.mu around each FeedPCM call.
func (s *wsStream) drain() {
	for {
		s.mu.Lock()
		if len(s.queued) == 0 || s.handle < 0 || !s.ready || s.peerGone || s.stopped {
			s.mu.Unlock()
			return
		}
		f := s.queued[0]
		s.queued = s.queued[1:]
		h := s.handle
		s.mu.Unlock()
		if err := asr.FeedPCM(h, f); err != nil {
			// Session ended under us; re-queue for the restarted
			// session and stop draining.
			s.mu.Lock()
			s.queued = append([][]byte{f}, s.queued...)
			s.mu.Unlock()
			return
		}
	}
}

// feedFrame queues one filtered 40ms frame and drains the queue
// into the live session when one is ready.
func (s *wsStream) feedFrame(frame []byte) {
	s.mu.Lock()
	if s.peerGone || s.stopped {
		s.mu.Unlock()
		return
	}
	s.queued = append(s.queued, frame)
	s.mu.Unlock()
	s.drain()
}

// feedBytes splits incoming bytes into 40ms frames, drops long
// silence runs through the live filter, and feeds the rest.
func (s *wsStream) feedBytes(data []byte) {
	s.mu.Lock()
	s.partial = append(s.partial, data...)
	var frames [][]byte
	for len(s.partial) >= silenceFrameBytes {
		frame := s.partial[:silenceFrameBytes]
		s.partial = s.partial[silenceFrameBytes:]
		if s.filter.feed(frame) {
			frames = append(frames, frame)
		}
	}
	s.mu.Unlock()
	for _, f := range frames {
		s.feedFrame(f)
	}
}

// startSession starts one Doubao live session for the current
// generation. It runs in its own goroutine: the handshake takes
// ~1s and the read loop must keep accepting frames meanwhile.
func (s *wsStream) startSession() {
	s.startSessionAttempt(false)
}

// startSessionAttempt is startSession with a bounded retry: a failed
// start is retried once, then the peer hears about it.
func (s *wsStream) startSessionAttempt(retried bool) {
	s.mu.Lock()
	if s.stopped || s.peerGone {
		s.mu.Unlock()
		return
	}
	s.gen++
	gen := s.gen
	s.starting = true
	done := make(chan struct{})
	s.startDone = done
	s.mu.Unlock()

	h, err := asr.StartSession("{}", func(ev asr.Event) { s.onEvent(ev, gen) })
	s.mu.Lock()
	s.starting = false
	close(done)
	if err != nil {
		s.mu.Unlock()
		log.Printf("[asr-server] ws session start failed: %v", err)
		s.handleStartFailure(retried)
		return
	}
	// A stop may have arrived during the handshake, or a fresh
	// {"type":"start"} may have moved the generation on; do not
	// use a session the peer no longer wants.
	if s.stopped || s.peerGone || gen != s.gen {
		s.mu.Unlock()
		asr.CloseSession(h)
		return
	}
	s.handle = h
	s.mu.Unlock()
}

// handleStartFailure retries a failed session start once, then
// reports the error to the peer (which shows a clear error; there
// is no record-then-transcribe path to fall back to).
func (s *wsStream) handleStartFailure(retried bool) {
	if retried {
		s.send(map[string]string{"type": "error", "message": "live session could not start", "code": "session_start_failed"})
		return
	}
	time.Sleep(time.Second)
	s.mu.Lock()
	if s.stopped || s.peerGone {
		s.mu.Unlock()
		return
	}
	s.mu.Unlock()
	s.startSessionAttempt(true)
}

// onEvent dispatches one Go session event to the peer. Events from
// a previous generation (a session that already ended) are ignored.
func (s *wsStream) onEvent(ev asr.Event, gen int) {
	s.mu.Lock()
	if gen != s.gen || s.peerGone {
		s.mu.Unlock()
		return
	}
	switch ev.Type {
	case "ready":
		s.ready = true
		s.mu.Unlock()
		s.send(map[string]any{"type": "ready", "session_id": ev.SessionID, "mode": ev.Mode})
		s.drain()
		return
	case "transcript":
		if ev.IsFinal {
			if ev.StableText != "" {
				s.stable = ev.StableText
			}
			confirmed := s.currentConfirmedLocked()
			stage := "stable"
			if ev.Stage == "session_final" {
				stage = "session_final"
			}
			text := ev.Text
			display := ev.Display
			s.mu.Unlock()
			s.send(map[string]any{"type": "transcript", "text": text, "display": display, "is_final": true, "is_interim": false, "stage": stage, "confirmed": confirmed})
			return
		}
		confirmed := s.currentConfirmedLocked()
		// The interim text is the session's cumulative text, which
		// includes the already-confirmed part. Send only the new
		// tail, so the overlay does not print the confirmed text
		// twice (once settled, once greyed).
		text := stripConfirmedPrefix(confirmed, ev.Text)
		display := stripConfirmedPrefix(confirmed, ev.Display)
		s.mu.Unlock()
		s.send(map[string]any{"type": "transcript", "text": text, "display": display, "is_final": false, "is_interim": true, "stage": "interim", "confirmed": confirmed})
		return
	case "error":
		msg := ev.Message
		s.mu.Unlock()
		log.Printf("[asr-server] ws session error: %s", msg)
		// Do not forward the error yet: the session is restarted
		// transparently below, and the peer only hears about it
		// if restarts run out.
		s.maybeRestart()
		return
	case "close":
		s.mu.Unlock()
		s.maybeRestart()
		return
	default:
		s.mu.Unlock()
		return
	}
}

// maybeRestart folds the finished session into the connection
// total and, after a short grace for trailing events, starts a
// new session — unless the peer already stopped or went away.
// Restarts are silent: the peer only gets an error if they run out.
func (s *wsStream) maybeRestart() {
	s.mu.Lock()
	if s.stopped || s.peerGone || s.starting || s.restartPend {
		s.mu.Unlock()
		return
	}
	if s.restarts >= maxStreamRestarts {
		s.mu.Unlock()
		s.send(map[string]string{"type": "error", "message": "live session ended repeatedly; try again", "code": "session_restart_limit"})
		return
	}
	s.restartPend = true
	gen := s.gen
	s.mu.Unlock()
	go func() {
		time.Sleep(restartGrace)
		s.mu.Lock()
		if gen != s.gen || s.stopped || s.peerGone || s.starting {
			s.restartPend = false
			s.mu.Unlock()
			return
		}
		s.absorbLocked()
		s.handle = -1
		s.ready = false
		s.stable = ""
		s.restarts++
		s.restartPend = false
		s.mu.Unlock()
		s.startSession()
	}()
}

// reset clears all per-recording state for a fresh {"type":"start"}.
func (s *wsStream) reset() {
	s.mu.Lock()
	if s.handle >= 0 {
		h := s.handle
		s.mu.Unlock()
		asr.CloseSession(h)
		s.mu.Lock()
	}
	s.gen++
	s.handle = -1
	s.ready = false
	s.stopped = false
	s.restarts = 0
	s.restartPend = false
	s.confirmed = ""
	s.stable = ""
	s.queued = nil
	s.partial = nil
	s.filter = liveSilenceFilter{}
	s.finalSent = false
	s.mu.Unlock()
}

// markPeerGone stops all session work after the peer disconnects.
func (s *wsStream) markPeerGone() {
	s.mu.Lock()
	s.peerGone = true
	h := s.handle
	s.handle = -1
	s.mu.Unlock()
	if h >= 0 {
		asr.CloseSession(h)
	}
}

// handleStop finishes the recording: flush the partial frame,
// wait for an in-flight or pending session start to settle, feed
// anything queued into the live session, finalise it, then send
// the total confirmed text as {"type":"final"}.
func (s *wsStream) handleStop() {
	s.mu.Lock()
	partial := s.partial
	s.partial = nil
	s.mu.Unlock()
	if len(partial) > 0 {
		// A trailing partial frame passes through unfiltered (it
		// is under 40ms, too short to end a session).
		s.feedFrame(partial)
	}
	// A session may have just ended and a restart may be pending;
	// give it a moment to start so the queued tail is transcribed
	// instead of dropped.
	deadline := time.Now().Add(1500 * time.Millisecond)
	for {
		s.mu.Lock()
		pend := s.restartPend
		starting := s.starting
		done := s.startDone
		s.mu.Unlock()
		if !pend && !starting {
			break
		}
		if starting && done != nil {
			select {
			case <-done:
			case <-time.After(3 * time.Second):
			}
			break
		}
		if time.Now().After(deadline) {
			break
		}
		time.Sleep(50 * time.Millisecond)
	}
	s.drain()
	s.mu.Lock()
	s.stopped = true
	h := s.handle
	s.mu.Unlock()
	if h >= 0 {
		// FinalizeSession waits up to 15s for SessionFinished;
		// the session_final event updates the stable text first.
		_ = asr.FinalizeSession(h)
	}
	s.mu.Lock()
	s.absorbLocked()
	total := s.confirmed
	sent := s.finalSent
	s.finalSent = true
	s.mu.Unlock()
	if !sent {
		s.send(map[string]any{"type": "final", "text": total})
		s.send(map[string]string{"type": "close"})
	}
	if h >= 0 {
		asr.CloseSession(h)
	}
}

// handleWS is the browser-relay protocol, kept for streaming clients:
// {"type":"start"} / binary PCM / {"type":"stop"} in, ready /
// transcript / final / error / close JSON out. One recording now
// survives session ends by restarting transparently (see wsStream),
// and long silences are compressed live so a pause never ends the
// recording early.
func handleWS(w http.ResponseWriter, r *http.Request) {
	conn, err := upgrader.Upgrade(w, r, nil)
	if err != nil {
		log.Printf("[asr-server] ws upgrade: %v", err)
		return
	}
	defer conn.Close()
	st := newWSStream(conn)
	defer st.markPeerGone()

	for {
		mt, data, err := conn.ReadMessage()
		if err != nil {
			return
		}
		if mt == websocket.BinaryMessage {
			buf := append([]byte(nil), data...)
			st.feedBytes(buf)
			continue
		}
		var msg struct {
			Type string `json:"type"`
		}
		if err := json.Unmarshal(data, &msg); err != nil {
			continue
		}
		switch msg.Type {
		case "start":
			st.reset()
			go st.startSession()
		case "stop":
			st.handleStop()
			return
		}
	}
}

func main() {
	addr := os.Getenv("ASR_SIDECAR_ADDR")
	if addr == "" {
		addr = "127.0.0.1:3101"
	}
	mux := http.NewServeMux()
	mux.HandleFunc("/healthz", handleHealthz)
	mux.HandleFunc("/postasr", requireKey(handlePostAsr))
	mux.HandleFunc("/ws", requireKey(handleWS))
	log.Printf("[asr-server] listening on %s (mock mode not ported; live ASR + RunPostAsr only)", addr)
	if err := http.ListenAndServe(addr, mux); err != nil {
		log.Fatalf("[asr-server] listen: %v", err)
	}
}
