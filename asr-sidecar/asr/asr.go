// Package asr is a pure Go client for the Doubao IME cloud ASR backend,
// ported from the Python doubaoime-asr reference and verified against the
// Rust cometix-asr addon. It uses no Node and no .node binary.
//
// API mirrors the Node addon (docs/cometix-asr.d.ts):
//
//	EnsureDid / GetCachedDid — device registration, cached in ~/.cache/cometix-asr/did.json
//	StartSession / FeedPCM / FinalizeSession / CloseSession / IsConnected
//	AudioFormat — 16kHz mono s16le, 640-sample frames
//	RunPostAsr — standalone post-ASR fmt/NER (see postasr.go)
package asr

import (
	"encoding/json"
	"errors"
	"fmt"
	"sync"
	"sync/atomic"
	"time"

	"github.com/gorilla/websocket"
)

// AudioFormat mirrors the Node addon's audioFormat().
type AudioFormat struct {
	SampleRate   int    `json:"sampleRate"`
	Channels     int    `json:"channels"`
	FrameSamples int    `json:"frameSamples"`
	FrameBytes   int    `json:"frameBytes"`
	Encoding     string `json:"encoding"`
	OpusBitrate  int    `json:"opusBitrate"`
}

// Format returns the audio format the client accepts.
func Format() AudioFormat {
	return AudioFormat{SampleRate: 16000, Channels: 1, FrameSamples: 640, FrameBytes: 1280, Encoding: "linear16", OpusBitrate: 64000}
}

// Event is one callback event from a session. Type is one of:
// "ready", "transcript", "error", "close", "debug".
type Event struct {
	Type          string `json:"type"`
	SessionID     string `json:"session_id,omitempty"`
	Mode          string `json:"mode,omitempty"`
	Text          string `json:"text,omitempty"`
	Display       string `json:"display,omitempty"`
	Stage         string `json:"stage,omitempty"`
	IsFinal       bool   `json:"is_final,omitempty"`
	IsInterim     bool   `json:"is_interim,omitempty"`
	IsVadFinished bool   `json:"is_vad_finished,omitempty"`
	StableText    string `json:"stable_text,omitempty"`
	PassCount     int    `json:"pass_count,omitempty"`
	Message       string `json:"message,omitempty"`
	Code          string `json:"code,omitempty"`
}

// SessionConfig is the JSON config accepted by StartSession.
// Only a subset of the Node addon's fields is used.
type SessionConfig struct {
	Did           string `json:"did,omitempty"`
	Mode          string `json:"mode,omitempty"` // "live" (default); mock mode is not ported
	EnablePostAsr bool   `json:"enablePostAsr,omitempty"`
	Context       string `json:"context,omitempty"`
}

type session struct {
	handle         int
	id             string // session UUID, also the protobuf request_id
	token          string // protobuf token (field 2): the public appkey
	conn           *websocket.Conn
	onEvent        func(Event)
	enc            *opusEncoder
	pcmBuf         []byte // partial PCM (< 1280 bytes) waiting for a full frame
	queued         [][]byte
	ready          bool
	connected      bool
	finalized      bool
	closed         bool
	closeEmitted   bool
	done           chan struct{}
	cumulativeText string // longest results[0] (cumulative formatted text) seen
	settledText    string // longest results[0] seen in a final (VAD-finished) payload
	frameNo        int32
	mu             sync.Mutex
	writeMu        sync.Mutex
}

var (
	handles   sync.Map // int -> *session
	nextHdl   atomic.Int32
	bootstrap sync.Mutex
)

// StartSession starts a live ASR session. configJSON is a SessionConfig JSON
// string (may be "{}"). Events are delivered to onEvent. It returns a
// handle for FeedPCM/FinalizeSession/CloseSession, mirroring startSession()
// in the Node addon.
func StartSession(configJSON string, onEvent func(Event)) (int, error) {
	var cfg SessionConfig
	if configJSON != "" {
		if err := json.Unmarshal([]byte(configJSON), &cfg); err != nil {
			return 0, fmt.Errorf("bad session config: %w", err)
		}
	}
	if cfg.Mode == "" {
		cfg.Mode = "live"
	}
	// Mock mode is intentionally not ported into chat2api: it only
	// simulated the lifecycle with an empty final transcript and no
	// network, and the user does not want it in this port.
	if cfg.Mode != "live" {
		return 0, fmt.Errorf("unsupported mode %q", cfg.Mode)
	}

	did, err := EnsureDid(false)
	if err != nil {
		return 0, fmt.Errorf("ensureDid: %w", err)
	}
	creds, err := loadCachedCredentials()
	if err != nil {
		return 0, err
	}
	_ = did
	token, err := fetchAppKey(creds)
	if err != nil {
		return 0, fmt.Errorf("settings token: %w", err)
	}

	s := &session{
		handle:  int(nextHdl.Add(1)),
		id:      newUUID(),
		token:   appKey, // protobuf token, as in the Rust capture
		onEvent: onEvent,
		done:    make(chan struct{}),
	}
	enc, err := newOpusEncoder(16000, 1)
	if err != nil {
		return 0, fmt.Errorf("opus encoder: %w", err)
	}
	s.enc = enc

	wsURL := buildWSURL(creds, token)
	dialer := websocket.Dialer{HandshakeTimeout: 15 * time.Second}
	header := wsHeaders()
	conn, _, err := dialer.Dial(wsURL, header)
	if err != nil {
		enc.Close()
		return 0, fmt.Errorf("dial asr websocket: %w", err)
	}
	s.conn = conn
	s.connected = true
	handles.Store(s.handle, s)

	s.emit(Event{Type: "debug", Message: "connecting wss://frontier-audio-ime-ws.doubao.com/ocean/api/v1/ws"})

	// StartTask, then wait for TaskStarted.
	if err := s.sendRequest("StartTask", "", nil, 0, true); err != nil {
		s.fail(err)
		return 0, err
	}
	if err := s.waitFor("TaskStarted", 15*time.Second); err != nil {
		s.fail(err)
		return 0, err
	}
	// StartSession with the captured config payload.
	payload, err := buildSessionPayload(creds, cfg.Context)
	if err != nil {
		s.fail(err)
		return 0, err
	}
	if err := s.sendRequest("StartSession", payload, nil, 0, true); err != nil {
		s.fail(err)
		return 0, err
	}
	if err := s.waitFor("SessionStarted", 15*time.Second); err != nil {
		s.fail(err)
		return 0, err
	}
	s.mu.Lock()
	s.ready = true
	queued := s.queued
	s.queued = nil
	s.mu.Unlock()
	s.emit(Event{Type: "ready", SessionID: s.id, Mode: "live"})
	s.emit(Event{Type: "debug", Message: "handshake OK; audio enabled"})
	for _, chunk := range queued {
		_ = s.feedLocked(chunk)
	}
	go s.readLoop()
	return s.handle, nil
}

func (s *session) emit(ev Event) {
	if s.onEvent != nil {
		// Deliver asynchronously so a slow callback can't block the read loop.
		ev2 := ev
		go s.onEvent(ev2)
	}
}

func (s *session) fail(err error) {
	s.emit(Event{Type: "error", Message: err.Error(), Code: "live_session"})
	s.Close()
}

// FeedPCM feeds s16le 16kHz mono PCM. Any length is accepted; it is buffered
// into 640-sample frames. Frames fed before the session is ready are queued
// (like the browser relay) so the first word is not clipped.
func FeedPCM(handle int, pcm []byte) error {
	v, ok := handles.Load(handle)
	if !ok {
		return errors.New("unknown session handle")
	}
	s := v.(*session)
	s.mu.Lock()
	defer s.mu.Unlock()
	if s.closed {
		return errors.New("session closed")
	}
	if !s.ready {
		cp := make([]byte, len(pcm))
		copy(cp, pcm)
		s.queued = append(s.queued, cp)
		return nil
	}
	return s.feedLocked(pcm)
}

// feedLocked encodes buffered PCM into 40ms Opus frames and sends them.
// Caller must hold s.mu.
func (s *session) feedLocked(pcm []byte) error {
	s.pcmBuf = append(s.pcmBuf, pcm...)
	for len(s.pcmBuf) >= 1280 {
		frame := s.pcmBuf[:1280]
		s.pcmBuf = append([]byte(nil), s.pcmBuf[1280:]...)
		opus, err := s.enc.Encode(frame)
		if err != nil {
			return fmt.Errorf("opus encode: %w", err)
		}
		s.frameNo++
		payload := fmt.Sprintf(`{"extra":{},"timestamp_ms":%d}`, time.Now().UnixMilli())
		if err := s.sendRequest("TaskRequest", payload, opus, s.frameNo, false); err != nil {
			return err
		}
	}
	return nil
}

// FinalizeSession sends FinishSession and waits for the final transcript /
// SessionFinished, then emits the session_final transcript and close.
func FinalizeSession(handle int) error {
	v, ok := handles.Load(handle)
	if !ok {
		return errors.New("unknown session handle")
	}
	s := v.(*session)
	s.mu.Lock()
	if s.closed || s.finalized {
		s.mu.Unlock()
		return nil
	}
	s.finalized = true
	// Flush a partial trailing frame as silence-padded audio, mirroring the
	// Python reference (pad final frame with zeros).
	if len(s.pcmBuf) > 0 {
		pad := make([]byte, 1280-len(s.pcmBuf))
		_ = s.feedLocked(pad)
		s.pcmBuf = nil
	}
	s.mu.Unlock()
	if err := s.sendRequest("FinishSession", "", nil, 0, true); err != nil {
		return err
	}
	// Wait for SessionFinished / close, bounded so a stalled server can't hang us.
	select {
	case <-s.done:
	case <-time.After(15 * time.Second):
	}
	return nil
}

// CloseSession closes the session and its WebSocket.
func CloseSession(handle int) {
	if v, ok := handles.Load(handle); ok {
		v.(*session).Close()
	}
}

// Close closes the session.
func (s *session) Close() {
	s.mu.Lock()
	if s.closed {
		s.mu.Unlock()
		return
	}
	s.closed = true
	s.connected = false
	s.mu.Unlock()
	handles.Delete(s.handle)
	if s.conn != nil {
		_ = s.conn.Close()
	}
	if s.enc != nil {
		s.enc.Close()
	}
	select {
	case <-s.done:
	default:
		close(s.done)
	}
}

// IsConnected reports whether the session's WebSocket is up.
func IsConnected(handle int) bool {
	v, ok := handles.Load(handle)
	if !ok {
		return false
	}
	s := v.(*session)
	s.mu.Lock()
	defer s.mu.Unlock()
	return s.connected && !s.closed
}

// RunPostAsr is implemented in postasr.go: standalone post-ASR fmt + NER,
// mirroring runPostAsr() in the Rust addon. Mock mode is not ported:
// see the note on SessionConfig.Mode above.
