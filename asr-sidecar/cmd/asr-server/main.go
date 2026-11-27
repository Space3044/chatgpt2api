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
//	                  server replies ready/transcript/error/close JSON.
//	POST /transcribe raw 16kHz mono s16le PCM body (max 60s), optional
//	                  ?postasr=1 to run the standalone RunPostAsr
//	                  (fmt + NER) on the final text. Returns
//	                  {"text","final_text","interim_count","post_asr"}.
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
	"fmt"
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

const (
	maxPCMSeconds = 60
	maxPCMBytes   = maxPCMSeconds * 16000 * 2 // 60s of 16kHz mono s16le
)

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

// runLiveSession streams one recorded utterance through a live ASR
// session and returns the final text and the interim count.
func runLiveSession(pcm []byte) (finalText string, interimCount int, err error) {
	var mu sync.Mutex
	done := make(chan struct{})
	var once sync.Once
	finish := func() { once.Do(func() { close(done) }) }

	h, err := asr.StartSession("{}", func(ev asr.Event) {
		mu.Lock()
		defer mu.Unlock()
		switch ev.Type {
		case "transcript":
			if ev.IsFinal {
				if ev.Stage == "session_final" || ev.Text != "" {
					if finalText == "" || strings.HasPrefix(ev.Text, finalText) {
						finalText = ev.Text
					} else if !strings.Contains(finalText, ev.Text) {
						finalText += ev.Text
					}
				}
				if ev.Stage == "session_final" {
					finish()
				}
			} else {
				interimCount++
			}
		case "error":
			finish()
			if err == nil {
				err = fmt.Errorf("asr session: %s", ev.Message)
			}
		case "close":
			finish()
		}
	})
	if err != nil {
		return "", 0, err
	}
	defer asr.CloseSession(h)

	for off := 0; off < len(pcm); off += 1280 {
		end := off + 1280
		if end > len(pcm) {
			end = len(pcm)
		}
		if ferr := asr.FeedPCM(h, pcm[off:end]); ferr != nil {
			return "", interimCount, ferr
		}
	}
	if err := asr.FinalizeSession(h); err != nil {
		return "", interimCount, err
	}
	select {
	case <-done:
	case <-time.After(20 * time.Second):
		return "", interimCount, fmt.Errorf("timed out waiting for final transcript")
	}
	mu.Lock()
	defer mu.Unlock()
	if err != nil {
		return "", interimCount, err
	}
	return finalText, interimCount, nil
}

func handleHealthz(w http.ResponseWriter, r *http.Request) {
	writeJSON(w, http.StatusOK, map[string]bool{"ok": true})
}

func handleTranscribe(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost {
		writeErr(w, http.StatusMethodNotAllowed, "method not allowed")
		return
	}
	pcm, err := io.ReadAll(io.LimitReader(r.Body, maxPCMBytes+1))
	if err != nil {
		writeErr(w, http.StatusBadRequest, "read pcm: "+err.Error())
		return
	}
	if len(pcm) == 0 {
		writeErr(w, http.StatusBadRequest, "empty pcm body")
		return
	}
	if len(pcm) > maxPCMBytes {
		writeErr(w, http.StatusRequestEntityTooLarge, "pcm longer than 60s")
		return
	}
	if len(pcm)%2 != 0 {
		writeErr(w, http.StatusBadRequest, "pcm must be s16le (even byte count)")
		return
	}
	log.Printf("[asr-server] transcribe: %d bytes (~%.1fs)", len(pcm), float64(len(pcm))/32000)
	text, interim, err := runLiveSession(pcm)
	if err != nil {
		log.Printf("[asr-server] transcribe failed: %v", err)
		writeErr(w, http.StatusBadGateway, err.Error())
		return
	}
	resp := map[string]any{
		"text":          text,
		"final_text":    text,
		"interim_count": interim,
		"post_asr":      nil,
	}
	if r.URL.Query().Get("postasr") == "1" {
		did := asr.GetCachedDid()
		cfg := fmt.Sprintf(`{"did":%q,"enableFmt":true,"enableNer":true}`, did)
		out, perr := asr.RunPostAsr(cfg, text)
		if perr != nil {
			log.Printf("[asr-server] postasr failed: %v", perr)
			resp["post_asr"] = map[string]string{"error": perr.Error()}
		} else {
			var parsed any
			if jerr := json.Unmarshal([]byte(out), &parsed); jerr == nil {
				resp["post_asr"] = parsed
				if m, ok := parsed.(map[string]any); ok {
					if ft, ok := m["final_text"].(string); ok && ft != "" {
						resp["final_text"] = ft
					}
				}
			}
		}
	}
	log.Printf("[asr-server] transcribe ok: %d chars, %d interim", len(text), interim)
	writeJSON(w, http.StatusOK, resp)
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

// handleWS is the browser-relay protocol, kept for streaming clients:
// {"type":"start"} / binary PCM / {"type":"stop"} in, ready /
// transcript / error / close JSON out. One live session per connection.
func handleWS(w http.ResponseWriter, r *http.Request) {
	conn, err := upgrader.Upgrade(w, r, nil)
	if err != nil {
		log.Printf("[asr-server] ws upgrade: %v", err)
		return
	}
	defer conn.Close()
	var mu sync.Mutex
	send := func(v any) {
		mu.Lock()
		defer mu.Unlock()
		_ = conn.WriteJSON(v)
	}
	var handle int = -1
	var ready bool
	var queued [][]byte
	closeSession := func() {
		if handle >= 0 {
			asr.CloseSession(handle)
			handle = -1
		}
		ready = false
		queued = nil
	}
	defer closeSession()

	for {
		mt, data, err := conn.ReadMessage()
		if err != nil {
			return
		}
		if mt == websocket.BinaryMessage {
			if handle < 0 {
				continue
			}
			buf := append([]byte(nil), data...)
			if !ready {
				queued = append(queued, buf)
				continue
			}
			if err := asr.FeedPCM(handle, buf); err != nil {
				send(map[string]string{"type": "error", "message": "feedPcm: " + err.Error()})
			}
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
			closeSession()
			// drainQueued feeds PCM that arrived before the session was
			// ready. It runs both from the ready callback and after
			// StartSession returns, because the ready event is delivered
			// asynchronously and may fire before the handle is assigned.
			drainQueued := func() {
				if handle < 0 || !ready {
					return
				}
				for _, b := range queued {
					_ = asr.FeedPCM(handle, b)
				}
				queued = nil
			}
			h, err := asr.StartSession("{}", func(ev asr.Event) {
				switch ev.Type {
				case "ready":
					ready = true
					send(map[string]any{"type": "ready", "session_id": ev.SessionID, "mode": ev.Mode})
					drainQueued()
				case "transcript":
					send(map[string]any{"type": "transcript", "text": ev.Text, "display": ev.Display, "is_final": ev.IsFinal, "is_interim": ev.IsInterim, "stage": ev.Stage})
				case "error":
					send(map[string]string{"type": "error", "message": ev.Message, "code": ev.Code})
				case "close":
					send(map[string]string{"type": "close"})
				}
			})
			if err != nil {
				send(map[string]string{"type": "error", "message": "startSession: " + err.Error()})
				continue
			}
			handle = h
			drainQueued()
		case "stop":
			if handle >= 0 {
				if err := asr.FinalizeSession(handle); err != nil {
					send(map[string]string{"type": "error", "message": "finalize: " + err.Error()})
				}
			}
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
	mux.HandleFunc("/transcribe", requireKey(handleTranscribe))
	mux.HandleFunc("/postasr", requireKey(handlePostAsr))
	mux.HandleFunc("/ws", requireKey(handleWS))
	log.Printf("[asr-server] listening on %s (mock mode not ported; live ASR + RunPostAsr only)", addr)
	if err := http.ListenAndServe(addr, mux); err != nil {
		log.Fatalf("[asr-server] listen: %v", err)
	}
}
