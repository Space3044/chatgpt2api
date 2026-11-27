package asr

import (
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
)

// Test servers stand in for the fmt / get_config / NER endpoints; the
// URLs are injected via the COMETIX_* env overrides the implementation
// reads per call, so no live network is involved.

func TestPostAsrConfigDefaults(t *testing.T) {
	var cfg postAsrConfig
	if err := json.Unmarshal([]byte(`{"did":"123"}`), &cfg); err != nil {
		t.Fatal(err)
	}
	if !cfg.EnableFmt {
		t.Fatal("enableFmt must default to true")
	}
	if cfg.EnableNer {
		t.Fatal("enableNer must default to false")
	}
	if cfg.SamiToken != nil {
		t.Fatal("missing samiToken must be nil (triggers get_config)")
	}
}

func TestPostAsrConfigEmptyTokenIsProvided(t *testing.T) {
	var cfg postAsrConfig
	if err := json.Unmarshal([]byte(`{"did":"123","samiToken":""}`), &cfg); err != nil {
		t.Fatal(err)
	}
	if cfg.SamiToken == nil || *cfg.SamiToken != "" {
		t.Fatal("samiToken \"\" must be a non-nil empty token")
	}
}

func TestPostAsrConfigDidRequired(t *testing.T) {
	if _, err := RunPostAsr(`{"enableFmt":true}`, "hi"); err == nil {
		t.Fatal("missing did must be a config error")
	}
	if _, err := RunPostAsr(`not json`, "hi"); err == nil {
		t.Fatal("bad config JSON must be an error")
	}
}

func TestFindPostAsrString(t *testing.T) {
	var v interface{}
	if err := json.Unmarshal([]byte(`{"Code":0,"Data":{"sami_token":"tok-1"}}`), &v); err != nil {
		t.Fatal(err)
	}
	got := findPostAsrString(v, []string{"sami_token", "samiToken", "token"})
	if got == nil || *got != "tok-1" {
		t.Fatalf("expected tok-1, got %+v", got)
	}
}

func TestRunPostAsrFmtOnlyEmptyData(t *testing.T) {
	fmtSrv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Header.Get("x-api-token") != "" {
			t.Error("fmt must not send x-api-* headers")
		}
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write([]byte(`{"code":0,"msg":"","data":""}`))
	}))
	defer fmtSrv.Close()
	t.Setenv("COMETIX_FMT_URL", fmtSrv.URL)
	t.Setenv("COMETIX_NER_URL", "http://127.0.0.1:1/unused")
	t.Setenv("COMETIX_CONFIG_URL", "http://127.0.0.1:1/unused")

	out, err := RunPostAsr(`{"did":"123","enableNer":false}`, "hello world")
	if err != nil {
		t.Fatal(err)
	}
	var res postAsrResult
	if err := json.Unmarshal([]byte(out), &res); err != nil {
		t.Fatal(err)
	}
	if res.FmtText == nil || *res.FmtText != "hello world" || res.FinalText != "hello world" {
		t.Fatalf("empty fmt data must keep the original text: %+v", res)
	}
	if len(res.Steps) != 1 || res.Steps[0] != "fmt ok" {
		t.Fatalf("expected [fmt ok], got %v", res.Steps)
	}
	if res.SamiTokenOK {
		t.Fatal("sami_token_ok must be false when NER is off")
	}
	if string(res.NerRaw) != "" && string(res.NerRaw) != "null" {
		t.Fatalf("ner_raw must be null when NER is off, got %s", res.NerRaw)
	}
}

func TestRunPostAsrFmtReturnsData(t *testing.T) {
	fmtSrv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write([]byte(`{"code":0,"data":"Hello world."}`))
	}))
	defer fmtSrv.Close()
	t.Setenv("COMETIX_FMT_URL", fmtSrv.URL)
	t.Setenv("COMETIX_NER_URL", "http://127.0.0.1:1/unused")
	t.Setenv("COMETIX_CONFIG_URL", "http://127.0.0.1:1/unused")

	out, err := RunPostAsr(`{"did":"123","enableNer":false}`, "hello world")
	if err != nil {
		t.Fatal(err)
	}
	var res postAsrResult
	if err := json.Unmarshal([]byte(out), &res); err != nil {
		t.Fatal(err)
	}
	if res.FinalText != "Hello world." {
		t.Fatalf("expected formatted text, got %q", res.FinalText)
	}
}

func TestRunPostAsrNerViaGetConfig(t *testing.T) {
	cfgSrv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write([]byte(`{"Code":0,"Data":{"sami_token":"tok-abc"}}`))
	}))
	defer cfgSrv.Close()
	var nerToken, nerUID string
	nerSrv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		nerToken = r.Header.Get("x-api-token")
		if r.Header.Get("x-api-resource-id") == "" || r.Header.Get("x-api-app-key") == "" || r.Header.Get("x-api-request-id") == "" {
			t.Error("NER must send x-api-resource-id / x-api-app-key / x-api-request-id")
		}
		var body map[string]interface{}
		_ = json.NewDecoder(r.Body).Decode(&body)
		if user, ok := body["user"].(map[string]interface{}); ok {
			nerUID, _ = user["uid"].(string)
		}
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write([]byte(`{"results":[{"text":"hello","words":[]}]}`))
	}))
	defer nerSrv.Close()
	fmtSrv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write([]byte(`{"code":0,"data":""}`))
	}))
	defer fmtSrv.Close()
	t.Setenv("COMETIX_FMT_URL", fmtSrv.URL)
	t.Setenv("COMETIX_NER_URL", nerSrv.URL)
	t.Setenv("COMETIX_CONFIG_URL", cfgSrv.URL)
	t.Setenv("COMETIX_APP_KEY", "test-app-key")

	out, err := RunPostAsr(`{"did":"123","enableNer":true,"uid":"u1"}`, "hello")
	if err != nil {
		t.Fatal(err)
	}
	var res postAsrResult
	if err := json.Unmarshal([]byte(out), &res); err != nil {
		t.Fatal(err)
	}
	if len(res.Steps) != 2 || res.Steps[0] != "fmt ok" || res.Steps[1] != "ner ok" {
		t.Fatalf("expected [fmt ok ner ok], got %v (errors %v)", res.Steps, res.Errors)
	}
	if !res.SamiTokenOK {
		t.Fatal("sami_token_ok must be true after a successful get_config + NER")
	}
	if nerToken != "tok-abc" {
		t.Fatalf("NER must use the get_config token, got %q", nerToken)
	}
	if nerUID != "u1" {
		t.Fatalf("NER user.uid must be passed through, got %q", nerUID)
	}
	if !strings.Contains(string(res.NerRaw), `"words":[]`) {
		t.Fatalf("ner_raw must be the verbatim NER response, got %s", res.NerRaw)
	}
}

// The post-ASR app key has no default in this repo: NER via get_config
// must fail clearly when COMETIX_APP_KEY is unset.
func TestRunPostAsrMissingAppKeyFailsClearly(t *testing.T) {
	t.Setenv("COMETIX_APP_KEY", "")
	t.Setenv("COMETIX_CONFIG_URL", "http://127.0.0.1:1/unused")
	t.Setenv("COMETIX_FMT_URL", "http://127.0.0.1:1/unused")
	t.Setenv("COMETIX_NER_URL", "http://127.0.0.1:1/unused")
	out, err := RunPostAsr(`{"did":"123","enableFmt":false,"enableNer":true}`, "hello")
	if err != nil {
		t.Fatal(err)
	}
	var res postAsrResult
	if err := json.Unmarshal([]byte(out), &res); err != nil {
		t.Fatal(err)
	}
	if len(res.Steps) != 1 || res.Steps[0] != "sami_token fail" {
		t.Fatalf("expected [sami_token fail], got %v", res.Steps)
	}
	if len(res.Errors) != 1 || !strings.Contains(res.Errors[0], "COMETIX_APP_KEY is not set") {
		t.Fatalf("expected a clear COMETIX_APP_KEY error, got %v", res.Errors)
	}
}

func TestRunPostAsrEmptyTokenSkipsGetConfig(t *testing.T) {
	cfgCalls := 0
	cfgSrv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		cfgCalls++
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write([]byte(`{"Data":{"sami_token":"tok-x"}}`))
	}))
	defer cfgSrv.Close()
	nerSrv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(401)
		_, _ = w.Write([]byte(`unauthorized`))
	}))
	defer nerSrv.Close()
	fmtSrv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write([]byte(`{"code":0,"data":""}`))
	}))
	defer fmtSrv.Close()
	t.Setenv("COMETIX_FMT_URL", fmtSrv.URL)
	t.Setenv("COMETIX_NER_URL", nerSrv.URL)
	t.Setenv("COMETIX_CONFIG_URL", cfgSrv.URL)
	t.Setenv("COMETIX_APP_KEY", "test-app-key")

	out, err := RunPostAsr(`{"did":"123","enableNer":true,"samiToken":""}`, "hello")
	if err != nil {
		t.Fatal(err)
	}
	if cfgCalls != 0 {
		t.Fatal("empty samiToken must NOT trigger get_config")
	}
	var res postAsrResult
	if err := json.Unmarshal([]byte(out), &res); err != nil {
		t.Fatal(err)
	}
	if len(res.Steps) != 2 || res.Steps[1] != "ner fail" {
		t.Fatalf("expected [fmt ok ner fail], got %v", res.Steps)
	}
	if !res.SamiTokenOK {
		t.Fatal("sami_token_ok must be true when a (empty) token was provided")
	}
}
