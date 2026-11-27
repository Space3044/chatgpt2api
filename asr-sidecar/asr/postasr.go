// RunPostAsr — standalone post-ASR: fmt + NER.
//
// Direct Go translation of the reconstructed Rust source supplied by the
// user (reconstructed/src/post_asr.rs, product_config.rs, config.rs,
// ttencrypt.rs). The endpoints and non-secret client constants are those
// in the supplied product_config.rs; they can be overridden at runtime
// with the COMETIX_* environment variables, as in the Rust source.
//
// Secret stripping (chat2api port): the post-ASR app key has NO default
// value in this repo. It must come from the COMETIX_APP_KEY environment
// variable; RunPostAsr fails clearly ("COMETIX_APP_KEY is not set")
// when a step that needs it is attempted without one. Only non-secret
// defaults stay in code: the aid/app id, the endpoint hosts, and the
// resource id. The live ASR session appkey in session.go is the public
// Doubao IME client constant from the captured frames, kept as-is.
//
// Flow, as in the Rust source:
//   - fmt is always attempted first (when enableFmt) and uses no token
//     and no x-api-* headers. The fmt service currently answers
//     {"code":0,"data":""}; an empty data means "keep the original text".
//   - get_config is only needed by NER. A missing/null samiToken triggers
//     it; an empty string counts as "provided" (it will fail auth) and
//     does NOT trigger get_config.
//   - NER posts the text with the short-lived token from get_config and
//     returns the response verbatim as ner_raw.
//   - The result JSON shape matches the live Rust addon:
//     {"input","fmt_text","final_text","ner_raw","sami_token_ok","steps","errors"}
//     with steps "fmt ok"/"ner ok" on success and "fmt fail"/"ner fail"/
//     "sami_token fail" on failure. sami_token_ok is
//     enableNer && a token was available (including an empty string).
//
// Live-session note: the supplied session.rs never calls post-ASR in the
// live path, so StartSession behaviour is unchanged. Mock mode is not
// ported into chat2api (see asr.go).
package asr

import (
	"bytes"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"os"
	"time"
)

// Post-ASR client constants, as in the supplied product_config.rs.
// They are overridable via COMETIX_APP_ID / COMETIX_FMT_URL /
// COMETIX_NER_URL / COMETIX_CONFIG_URL. The app key is deliberately
// NOT here: see postAsrAppKey below.
const (
	postAsrAppID          = "401734"
	postAsrResourceID     = "asr.user.context"
	postAsrUserAgent      = "DoubaoIME/1.3.14 (research; doubao-asr)"
	postAsrAppVersion     = "1.3.14"
	postAsrAppName        = "doubaoime"
	postAsrOS             = "Android"
	postAsrPlatform       = "android"
	postAsrFmtURLConst    = "https://ime.doubao.com/api/v1/asr/fmt"
	postAsrNerURLConst    = "https://speech.bytedance.com/api/v3/context/ime/ner"
	postAsrConfigURLConst = "https://ime.oceancloudapi.com/api/v1/user/get_config"
	postAsrHTTPTimeout    = 30 * time.Second
)

func postAsrEnv(key, def string) string {
	if v := os.Getenv(key); v != "" {
		return v
	}
	return def
}

// postAsrAppKey returns the post-ASR app key from COMETIX_APP_KEY.
// There is no default value in this repo: the key must be supplied
// via the environment (on the VPS, in /opt/chatgpt2api/.env).
func postAsrAppKey() (string, error) {
	if v := os.Getenv("COMETIX_APP_KEY"); v != "" {
		return v, nil
	}
	return "", fmt.Errorf("COMETIX_APP_KEY is not set")
}

func postAsrDefaultAppID() string { return postAsrEnv("COMETIX_APP_ID", postAsrAppID) }
func postAsrFmtURL() string       { return postAsrEnv("COMETIX_FMT_URL", postAsrFmtURLConst) }
func postAsrNerURL() string       { return postAsrEnv("COMETIX_NER_URL", postAsrNerURLConst) }
func postAsrConfigURL() string    { return postAsrEnv("COMETIX_CONFIG_URL", postAsrConfigURLConst) }

// postAsrConfig is the JSON config accepted by RunPostAsr. The five fields
// are did / enableFmt / enableNer / samiToken / uid, as in the supplied
// config.rs. did is required; enableFmt defaults to true, enableNer to
// false. samiToken is a *string so missing/null (fetch via get_config)
// can be told apart from "" (provided, will fail auth).
type postAsrConfig struct {
	Did       string
	EnableFmt bool
	EnableNer bool
	SamiToken *string
	UID       string
}

func (c *postAsrConfig) UnmarshalJSON(data []byte) error {
	var raw struct {
		Did       *string `json:"did"`
		EnableFmt *bool   `json:"enableFmt"`
		EnableNer *bool   `json:"enableNer"`
		SamiToken *string `json:"samiToken"`
		UID       string  `json:"uid"`
	}
	// Distinguish missing/null samiToken from "": encoding/json sets a
	// *string to nil for both missing and null, and to a pointer to ""
	// for "". That is exactly the distinction the Rust Option<String>
	// makes, so a plain decode is sufficient.
	if err := json.Unmarshal(data, &raw); err != nil {
		return err
	}
	if raw.Did == nil || *raw.Did == "" {
		return fmt.Errorf("missing field `did`")
	}
	c.Did = *raw.Did
	c.EnableFmt = true
	if raw.EnableFmt != nil {
		c.EnableFmt = *raw.EnableFmt
	}
	if raw.EnableNer != nil {
		c.EnableNer = *raw.EnableNer
	}
	c.SamiToken = raw.SamiToken
	c.UID = raw.UID
	return nil
}

// postAsrResult is the JSON returned by RunPostAsr, field order and names
// as in the Rust PostAsrResult. fmt_text / ner_raw are null when the
// corresponding step was not attempted or failed.
type postAsrResult struct {
	Input       string          `json:"input"`
	FmtText     *string         `json:"fmt_text"`
	FinalText   string          `json:"final_text"`
	NerRaw      json.RawMessage `json:"ner_raw"`
	SamiTokenOK bool            `json:"sami_token_ok"`
	Steps       []string        `json:"steps"`
	Errors      []string        `json:"errors"`
}

// RunPostAsr is the standalone post-ASR step (fmt + NER) from the Rust
// addon. configJSON is a postAsrConfig JSON string; text is already
// recognised text. It returns the PostAsrResult JSON string. A config
// parse error is returned as an error; fmt/NER/get_config failures are
// reported in the result's steps/errors, as in the Rust source.
func RunPostAsr(configJSON string, text string) (string, error) {
	var cfg postAsrConfig
	if err := json.Unmarshal([]byte(configJSON), &cfg); err != nil {
		return "", fmt.Errorf("post config: %w", err)
	}
	result := runPostAsr(cfg, text)
	out, err := json.Marshal(result)
	if err != nil {
		return "", err
	}
	return string(out), nil
}

func runPostAsr(cfg postAsrConfig, text string) postAsrResult {
	steps := []string{}
	errors := []string{}
	var fmtText *string
	var nerRaw json.RawMessage
	token := cfg.SamiToken

	// fmt first; it uses no token at all.
	if cfg.EnableFmt {
		v, err := postAsrFmt(&cfg, text)
		if err != nil {
			steps = append(steps, "fmt fail")
			errors = append(errors, err.Error())
		} else {
			steps = append(steps, "fmt ok")
			fmtText = &v
		}
	}

	// get_config is only needed by NER, and only when no token was given.
	if cfg.EnableNer && token == nil {
		t, err := postAsrGetConfig(&cfg)
		if err != nil {
			steps = append(steps, "sami_token fail")
			errors = append(errors, err.Error())
		} else {
			token = t
		}
	}

	if cfg.EnableNer {
		if token != nil {
			v, err := postAsrNer(&cfg, *token, text)
			if err != nil {
				steps = append(steps, "ner fail")
				errors = append(errors, err.Error())
			} else {
				steps = append(steps, "ner ok")
				nerRaw = v
			}
		}
	}

	finalText := text
	if fmtText != nil {
		finalText = *fmtText
	}
	return postAsrResult{
		Input:       text,
		FmtText:     fmtText,
		FinalText:   finalText,
		NerRaw:      nerRaw,
		SamiTokenOK: cfg.EnableNer && token != nil,
		Steps:       steps,
		Errors:      errors,
	}
}

// postAsrGetConfig exchanges the app key for a short-lived token.
// Body: {"sami_app_key": <app key>}. The token is found recursively
// under sami_token / samiToken / token in data/Data/result/Result.
func postAsrGetConfig(cfg *postAsrConfig) (*string, error) {
	appKey, err := postAsrAppKey()
	if err != nil {
		return nil, fmt.Errorf("get_config: %w", err)
	}
	body := map[string]interface{}{"sami_app_key": appKey}
	status, statusText, resp, err := postAsrPostJSON(postAsrConfigURL(), body, cfg.Did, nil)
	if err != nil {
		return nil, fmt.Errorf("get_config: %w", err)
	}
	if status < 200 || status >= 300 {
		return nil, fmt.Errorf("get_config HTTP %s: %s", statusText, resp)
	}
	var value interface{}
	if err := json.Unmarshal([]byte(resp), &value); err != nil {
		return nil, fmt.Errorf("get_config json: %v; body=%s", err, resp)
	}
	return findPostAsrString(value, []string{"sami_token", "samiToken", "token"}), nil
}

// postAsrFmt posts the text for formatting. The service currently answers
// {"code":0,"data":""}; an empty data means keep the original text.
func postAsrFmt(cfg *postAsrConfig, text string) (string, error) {
	body := map[string]interface{}{
		"app_id":      postAsrDefaultAppID(),
		"app_version": postAsrAppVersion,
		"did":         cfg.Did,
		"os_type":     postAsrOS,
		"text":        text,
	}
	status, statusText, resp, err := postAsrPostJSON(postAsrFmtURL(), body, cfg.Did, nil)
	if err != nil {
		return "", fmt.Errorf("fmt: %w", err)
	}
	if status < 200 || status >= 300 {
		return "", fmt.Errorf("fmt HTTP %s: %s", statusText, resp)
	}
	var value interface{}
	if err := json.Unmarshal([]byte(resp), &value); err != nil {
		return "", fmt.Errorf("fmt json: %v; body=%s", err, resp)
	}
	if obj, ok := value.(map[string]interface{}); ok {
		if code, ok := obj["code"].(float64); ok && code != 0 {
			return "", fmt.Errorf("fmt code %d: %s", int64(code), resp)
		}
		if data, ok := obj["data"].(string); ok && data != "" {
			return data, nil
		}
	}
	return text, nil
}

// postAsrNer posts the text for entity extraction and returns the
// response verbatim as ner_raw.
func postAsrNer(cfg *postAsrConfig, token, text string) (json.RawMessage, error) {
	body := map[string]interface{}{
		"additions": map[string]interface{}{},
		"text":      text,
		"user": map[string]interface{}{
			"app_name":           postAsrAppName,
			"app_version":        postAsrAppVersion,
			"did":                cfg.Did,
			"experience_improve": false,
			"platform":           postAsrPlatform,
			"sdk_version":        "",
			"uid":                cfg.UID,
		},
	}
	status, statusText, resp, err := postAsrPostJSON(postAsrNerURL(), body, cfg.Did, &token)
	if err != nil {
		return nil, fmt.Errorf("ner: %w", err)
	}
	if status < 200 || status >= 300 {
		return nil, fmt.Errorf("ner HTTP %s: %s", statusText, resp)
	}
	var value interface{}
	if err := json.Unmarshal([]byte(resp), &value); err != nil {
		return nil, fmt.Errorf("ner json: %v; body=%s", err, resp)
	}
	return json.RawMessage(resp), nil
}

// postAsrPostJSON posts a JSON body with the headers from the Rust
// post_json. Auth headers (x-api-*) are only added when token != nil.
func postAsrPostJSON(url string, body interface{}, did string, token *string) (int, string, string, error) {
	payload, err := json.Marshal(body)
	if err != nil {
		return 0, "", "", err
	}
	req, err := http.NewRequest("POST", url, bytes.NewReader(payload))
	if err != nil {
		return 0, "", "", err
	}
	req.Header.Set("Content-Type", "application/json")
	req.Header.Set("User-Agent", postAsrUserAgent)
	req.Header.Set("Accept", "*/*")
	req.Header.Set("app_version", postAsrAppVersion)
	req.Header.Set("app_id", postAsrDefaultAppID())
	req.Header.Set("os_type", postAsrOS)
	req.Header.Set("did", did)
	if token != nil {
		appKey, err := postAsrAppKey()
		if err != nil {
			return 0, "", "", err
		}
		req.Header.Set("x-api-resource-id", postAsrResourceID)
		req.Header.Set("x-api-app-key", appKey)
		req.Header.Set("x-api-token", *token)
		req.Header.Set("x-api-request-id", newUUID())
		req.Header.Set("x-api-sequence", "-1")
	}
	client := &http.Client{Timeout: postAsrHTTPTimeout}
	resp, err := client.Do(req)
	if err != nil {
		return 0, "", "", err
	}
	defer resp.Body.Close()
	data, err := io.ReadAll(resp.Body)
	if err != nil {
		return 0, "", "", err
	}
	// resp.Status is "401 Unauthorized", matching the Rust StatusCode
	// display used in its "… HTTP {status}: …" error strings.
	return resp.StatusCode, resp.Status, string(data), nil
}

// findPostAsrString searches a parsed JSON value for the first non-empty
// string under one of keys, recursing into data/Data/result/Result.
func findPostAsrString(value interface{}, keys []string) *string {
	obj, ok := value.(map[string]interface{})
	if !ok {
		return nil
	}
	for _, k := range keys {
		if found, ok := obj[k].(string); ok && found != "" {
			return &found
		}
	}
	for _, k := range []string{"data", "Data", "result", "Result"} {
		if child, ok := obj[k]; ok {
			if found := findPostAsrString(child, keys); found != nil {
				return found
			}
		}
	}
	return nil
}
