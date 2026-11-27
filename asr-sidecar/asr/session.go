package asr

import (
	"crypto/rand"
	"encoding/json"
	"fmt"
	"net/http"
	"net/url"
	"os"
	"strings"
	"time"

	"github.com/gorilla/websocket"
)

// debugLogResultJSON appends a raw result_json payload to
// /tmp/cometix-asr-result.jsonl when COMETIX_ASR_DEBUG_JSON is set.
// result_json contains no credentials; it is only ever written to a local
// file for protocol debugging, never printed to stdout.
func debugLogResultJSON(s string) {
	f, err := os.OpenFile("/tmp/cometix-asr-result.jsonl", os.O_APPEND|os.O_CREATE|os.O_WRONLY, 0o600)
	if err != nil {
		return
	}
	defer f.Close()
	_, _ = f.WriteString(s + "\n")
}

// appKey is the public Doubao IME client constant, present in the captured
// frames. The per-session URL token comes from the settings API (fetchAppKey).
const appKey = "OrnqKvSSrs"

func newUUID() string {
	var b [16]byte
	_, _ = rand.Read(b[:])
	b[6] = (b[6] & 0x0f) | 0x40
	b[8] = (b[8] & 0x3f) | 0x80
	return fmt.Sprintf("%x-%x-%x-%x-%x", b[0:4], b[4:6], b[6:8], b[8:10], b[10:16])
}

func wsHeaders() http.Header {
	h := http.Header{}
	h.Set("x-ss-dp", "401734")
	h.Set("Origin", "wss://frontier-audio-ime-ws.doubao.com")
	h.Set("x-keepalive-timeout", "3600")
	h.Set("x-keepalive-interval", "3")
	h.Set("x-custom-keepalive", "true")
	h.Set("proto-version", "v2")
	return h
}

// buildWSURL mirrors the URL the Rust addon connects to (verified by capture).
// token is the settings app_key for this device.
func buildWSURL(creds *credentials, token string) string {
	q := url.Values{}
	q.Set("aid", "401734")
	q.Set("app_name", "doubaoime")
	q.Set("channel", "official")
	q.Set("device_brand", "Xiaomi")
	q.Set("device_id", creds.Did)
	q.Set("device_platform", "android")
	q.Set("device_type", "Mi Note 10")
	q.Set("did", creds.Did)
	q.Set("iid", creds.InstallID)
	q.Set("install_id", creds.InstallID)
	q.Set("os_version", "12")
	q.Set("update_version_code", "100314018")
	q.Set("version_code", "100314018")
	q.Set("version_name", "1.3.14")
	q.Set("appkey", appKey)
	q.Set("token", token)
	return "wss://frontier-audio-ime-ws.doubao.com/ocean/api/v1/ws?" + q.Encode()
}

// sessionPayload is the StartSession JSON, verbatim from the captured frame
// (docs/cap_msg_2.bin), with only did and context substituted.
type sessionPayload struct {
	AudioInfo struct {
		Channel    int    `json:"channel"`
		Format     string `json:"format"`
		SampleRate int    `json:"sample_rate"`
	} `json:"audio_info"`
	EnablePunctuation     bool         `json:"enable_punctuation"`
	EnableSpeechRejection bool         `json:"enable_speech_rejection"`
	Extra                 sessionExtra `json:"extra"`
}

type sessionExtra struct {
	AppName                          string        `json:"app_name"`
	AppVersion                       string        `json:"app_version"`
	AsrParams                        string        `json:"asr_params"`
	CellCompressRate                 int           `json:"cell_compress_rate"`
	Context                          string        `json:"context"`
	Did                              string        `json:"did"`
	DisableUserWords                 bool          `json:"disable_user_words"`
	EnableAsrThreepass               bool          `json:"enable_asr_threepass"`
	EnableAsrTwopass                 bool          `json:"enable_asr_twopass"`
	EnablePrintChinese               bool          `json:"enable_print_chinese"`
	EndSmoothWindowMs                int           `json:"end_smooth_window_ms"`
	FinishWaitOfflineTime            int           `json:"finish_wait_offline_time"`
	InputMode                        string        `json:"input_mode"`
	JoinUserExperienceImproveProgram bool          `json:"join_user_experience_improve_program"`
	MaxWaitSwitchOfflineTime         int           `json:"max_wait_switch_offline_time"`
	NetworkChange                    networkChange `json:"network_change"`
	OfflineWaitOnlineIntervalTime    int           `json:"offline_wait_online_interval_time"`
	OfflineWaitOnlineTime            int           `json:"offline_wait_online_time"`
	OS                               string        `json:"os"`
	OSVersion                        string        `json:"os_version"`
	RemoveSpaceBetweenHanEng         bool          `json:"remove_space_between_han_eng"`
	RemoveSpaceBetweenHanNum         bool          `json:"remove_space_between_han_num"`
	RetryServerCode                  []int         `json:"retry_server_code"`
	S2aSendCommands                  []string      `json:"s2a_send_commands"`
	S2aSendEnable                    bool          `json:"s2a_send_enable"`
	StrongDdc                        bool          `json:"strong_ddc"`
	UseTwopassRetry                  bool          `json:"use_twopass_retry"`
}

type networkChange struct {
	SwitchNetworkPingTimeout      int `json:"switch_network_ping_timeout"`
	SwitchNetworkQualityThreshold int `json:"switch_network_quality_threshold"`
	SwitchNetworkRttThreshold     int `json:"switch_network_rtt_threshold"`
}

func buildSessionPayload(creds *credentials, context string) (string, error) {
	var p sessionPayload
	p.AudioInfo.Channel = 1
	p.AudioInfo.Format = "speech_opus"
	p.AudioInfo.SampleRate = 16000
	p.EnablePunctuation = true
	p.EnableSpeechRejection = false
	e := &p.Extra
	e.AppName = "com.bytedance.android.doubaoime"
	e.AppVersion = "1.3.14"
	e.AsrParams = `{"enable_global_tracking": true}`
	e.CellCompressRate = 8
	e.Context = context
	e.Did = creds.Did
	e.DisableUserWords = false
	e.EnableAsrThreepass = true
	e.EnableAsrTwopass = true
	e.EnablePrintChinese = false
	e.EndSmoothWindowMs = 800
	e.FinishWaitOfflineTime = 1000
	e.InputMode = "tool"
	e.JoinUserExperienceImproveProgram = false
	e.MaxWaitSwitchOfflineTime = 1000
	e.NetworkChange = networkChange{SwitchNetworkPingTimeout: 2000, SwitchNetworkQualityThreshold: 4, SwitchNetworkRttThreshold: 273}
	e.OfflineWaitOnlineIntervalTime = 5000
	e.OfflineWaitOnlineTime = 5000
	e.OS = "Android"
	e.OSVersion = "14"
	e.RemoveSpaceBetweenHanEng = false
	e.RemoveSpaceBetweenHanNum = false
	e.RetryServerCode = []int{40100000, 40100004, 50000000, 50000104, 50700000}
	e.S2aSendCommands = []string{"帮我发送"}
	e.S2aSendEnable = false
	e.StrongDdc = false
	e.UseTwopassRetry = true
	b, err := json.Marshal(p)
	if err != nil {
		return "", err
	}
	return string(b), nil
}

// sendRequest writes one AsrRequest. withToken adds field 2 (StartTask,
// StartSession, FinishSession); TaskRequest omits it, per the capture.
func (s *session) sendRequest(method, payload string, audio []byte, frameNo int32, withToken bool) error {
	token := ""
	if withToken {
		token = s.token
	}
	data := marshalRequest(token, method, payload, audio, s.id, frameNo)
	s.writeMu.Lock()
	defer s.writeMu.Unlock()
	if s.conn == nil {
		return fmt.Errorf("session %d: no websocket", s.handle)
	}
	return s.conn.WriteMessage(websocket.BinaryMessage, data)
}

// waitFor reads responses synchronously (used during the handshake, before
// readLoop starts) until a response with the given message_type arrives.
func (s *session) waitFor(want string, timeout time.Duration) error {
	deadline := time.Now().Add(timeout)
	for {
		remaining := time.Until(deadline)
		if remaining <= 0 {
			return fmt.Errorf("timeout waiting for %s", want)
		}
		_ = s.conn.SetReadDeadline(time.Now().Add(remaining))
		_, data, err := s.conn.ReadMessage()
		if err != nil {
			return fmt.Errorf("waiting for %s: %w", want, err)
		}
		resp, err := parseResponse(data)
		if err != nil {
			continue
		}
		if resp.MessageType == "TaskFailed" || resp.MessageType == "SessionFailed" {
			return fmt.Errorf("%s: code=%d %s", resp.MessageType, resp.StatusCode, resp.StatusMessage)
		}
		if resp.MessageType == want {
			if resp.StatusCode != 0 && resp.StatusCode != 20000000 {
				return fmt.Errorf("%s: unexpected status code %d %s", want, resp.StatusCode, resp.StatusMessage)
			}
			s.emit(Event{Type: "debug", Message: want + " OK code=" + fmt.Sprint(resp.StatusCode)})
			return nil
		}
		// Any other message during handshake (e.g. a result) is handled
		// by the read loop once it starts; ignore it here.
		_ = resp
	}
}

// readLoop runs after the handshake and dispatches server messages as events.
func (s *session) readLoop() {
	defer func() {
		s.mu.Lock()
		wasFinalized := s.finalized
		s.mu.Unlock()
		_ = wasFinalized
		s.emitCloseOnce()
	}()
	for {
		_, data, err := s.conn.ReadMessage()
		if err != nil {
			if !s.isClosed() {
				s.emit(Event{Type: "error", Message: "ws read: " + err.Error(), Code: "live_session"})
			}
			return
		}
		resp, err := parseResponse(data)
		if err != nil {
			continue
		}
		s.handleResponse(resp)
	}
}

func (s *session) isClosed() bool {
	s.mu.Lock()
	defer s.mu.Unlock()
	return s.closed
}

func (s *session) emitCloseOnce() {
	s.mu.Lock()
	if s.closeEmitted {
		s.mu.Unlock()
		return
	}
	s.closeEmitted = true
	s.mu.Unlock()
	s.emit(Event{Type: "close"})
	select {
	case <-s.done:
	default:
		close(s.done)
	}
}

func (s *session) handleResponse(resp *response) {
	switch resp.MessageType {
	case "TaskFailed", "SessionFailed":
		s.emit(Event{Type: "error", Message: resp.StatusMessage, Code: fmt.Sprint(resp.StatusCode)})
		return
	case "SessionFinished":
		s.emitSessionFinal()
		s.emitCloseOnce()
		return
	}
	if resp.ResultJSON == "" {
		return // heartbeat / lifecycle message without results
	}
	if os.Getenv("COMETIX_ASR_DEBUG_JSON") != "" {
		debugLogResultJSON(resp.ResultJSON)
	}
	rj, err := parseResultJSON(resp.ResultJSON)
	if err != nil {
		return
	}
	if len(rj.Results) == 0 {
		return // heartbeat (results:null)
	}
	text := rj.text()
	if text == "" {
		return // VAD start (empty text)
	}
	if rj.isFinal() {
		ft := rj.finalText()
		if ft == "" {
			ft = text
		}
		s.mu.Lock()
		if s.finalText == "" {
			s.finalText = ft
		} else if strings.HasPrefix(ft, s.finalText) {
			s.finalText = ft
		} else if !strings.Contains(s.finalText, ft) {
			s.finalText += ft
		}
		s.stable = s.finalText
		s.mu.Unlock()
		// Stage "stable" here; the matching "session_final" event is
		// emitted when SessionFinished arrives (emitSessionFinal), as in
		// the Rust addon.
		s.emit(Event{Type: "transcript", Text: ft, Display: ft, Stage: "stable", IsFinal: true, IsInterim: false, IsVadFinished: true, StableText: s.finalText, PassCount: 2})
		return
	}
	// Interim: the server sends cumulative formatted text in results[0].
	s.emit(Event{Type: "transcript", Text: text, Display: text, Stage: "interim", IsFinal: false, IsInterim: true, IsVadFinished: false, PassCount: 2})
}

func (s *session) emitSessionFinal() {
	s.mu.Lock()
	ft := s.finalText
	s.mu.Unlock()
	if ft == "" {
		return
	}
	s.emit(Event{Type: "transcript", Text: ft, Display: ft, Stage: "session_final", IsFinal: true, IsInterim: false, IsVadFinished: true, StableText: ft, PassCount: 1})
}
