package asr

import (
	"bytes"
	"crypto/md5"
	"crypto/rand"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"os"
	"path/filepath"
	"strconv"
	"strings"
	"time"
)

// Device registration + settings token, ported from the Python
// doubaoime-asr reference (device.py) and verified live on the VPS:
// device_register returns device_id/install_id on the first attempt with
// the body shape below; settings v3 returns a stable per-device app_key.

const (
	registerURL = "https://log.snssdk.com/service/2/device_register/"
	settingsURL = "https://is.snssdk.com/service/settings/v3/"
	userAgent   = "com.bytedance.android.doubaoime/100102018 (Linux; U; Android 16; en_US; Pixel 7 Pro; Build/BP2A.250605.031.A2; Cronet/TTNetVersion:94cf429a 2025-11-17 QuicVersion:1f89f732 2025-05-08)"
)

// credentials is one registered device. Field names match the Rust addon's
// ~/.cache/cometix-asr/did.json so the cache is interchangeable.
type credentials struct {
	Did         string `json:"did"`
	InstallID   string `json:"install_id"`
	SSID        string `json:"ssid,omitempty"`
	Cdid        string `json:"cdid"`
	Openudid    string `json:"openudid"`
	Clientudid  string `json:"clientudid"`
	DeviceBrand string `json:"device_brand"`
	DeviceType  string `json:"device_type"`
	OSVersion   string `json:"os_version"`
	TS          int64  `json:"ts,omitempty"`
	Source      string `json:"source,omitempty"`
}

func cachePath() (string, error) {
	home, err := os.UserHomeDir()
	if err != nil {
		return "", err
	}
	return filepath.Join(home, ".cache", "cometix-asr", "did.json"), nil
}

func loadCachedCredentials() (*credentials, error) {
	p, err := cachePath()
	if err != nil {
		return nil, err
	}
	b, err := os.ReadFile(p)
	if err != nil {
		return nil, fmt.Errorf("no cached device credentials: %w", err)
	}
	var c credentials
	if err := json.Unmarshal(b, &c); err != nil {
		return nil, fmt.Errorf("bad cached device credentials: %w", err)
	}
	if c.Did == "" || c.Cdid == "" {
		return nil, fmt.Errorf("cached device credentials are incomplete")
	}
	return &c, nil
}

// GetCachedDid returns the cached device id, or "" if none is cached.
// Mirrors getCachedDid() in the Node addon.
func GetCachedDid() string {
	c, err := loadCachedCredentials()
	if err != nil {
		return ""
	}
	return c.Did
}

// EnsureDid returns a server-issued device id, registering a fresh device
// when force is true or nothing is cached. Mirrors ensureDid() in the Node
// addon. The cache file is ~/.cache/cometix-asr/did.json.
func EnsureDid(force bool) (string, error) {
	if !force {
		if c, err := loadCachedCredentials(); err == nil && c.Did != "" {
			return c.Did, nil
		}
	}
	c, err := registerDevice()
	if err != nil {
		return "", err
	}
	if err := saveCredentials(c); err != nil {
		return "", err
	}
	return c.Did, nil
}

func saveCredentials(c *credentials) error {
	p, err := cachePath()
	if err != nil {
		return err
	}
	if err := os.MkdirAll(filepath.Dir(p), 0o700); err != nil {
		return err
	}
	b, err := json.Marshal(c)
	if err != nil {
		return err
	}
	return os.WriteFile(p, b, 0o600)
}

func newUUIDStr() string { return newUUID() }

func newHexID(n int) string {
	b := make([]byte, n)
	_, _ = rand.Read(b)
	return hex.EncodeToString(b)
}

type registerResponse struct {
	DeviceID     json.Number `json:"device_id"`
	InstallID    json.Number `json:"install_id"`
	ServerTime   int64       `json:"server_time"`
	DeviceIDStr  string      `json:"device_id_str"`
	InstallIDStr string      `json:"install_id_str"`
}

func registerDevice() (*credentials, error) {
	cdid := newUUIDStr()
	openudid := newHexID(8) // 16 hex chars
	clientudid := newUUIDStr()
	now := time.Now().UnixMilli()

	header := map[string]interface{}{
		"display_name":          "Doubao IME",
		"device_id":             0,
		"install_id":            0,
		"device_platform":       "android",
		"device_type":           "Pixel 7 Pro",
		"device_brand":          "google",
		"device_model":          "Pixel 7 Pro",
		"os":                    "android",
		"os_version":            "16",
		"os_api":                "34",
		"resolution":            "1080*2400",
		"dpi":                   "420",
		"language":              "zh",
		"timezone":              8,
		"access":                "wifi",
		"app_name":              "oime",
		"version_code":          100102018,
		"version_name":          "1.1.2",
		"channel":               "official",
		"package":               "com.bytedance.android.doubaoime",
		"openudid":              openudid,
		"clientudid":            clientudid,
		"cdid":                  cdid,
		"update_version_code":   100102018,
		"manifest_version_code": 100102018,
		"rom":                   "UP1A.231005.007",
		"rom_version":           "UP1A.231005.007",
	}
	bodyMap := map[string]interface{}{
		"magic_tag": "ss_app_log",
		"header":    header,
		"gen_time":  now,
	}
	body, err := json.Marshal(bodyMap)
	if err != nil {
		return nil, err
	}
	params := url.Values{}
	params.Set("device_platform", "android")
	params.Set("os", "android")
	params.Set("ssmix", "a")
	params.Set("_rticket", strconv.FormatInt(now, 10))
	params.Set("cdid", cdid)
	params.Set("aid", "401734")
	params.Set("app_name", "oime")
	params.Set("version_code", "100102018")
	params.Set("channel", "official")
	params.Set("device_type", "Pixel 7 Pro")
	params.Set("device_brand", "google")
	params.Set("resolution", "1080*2400")
	params.Set("dpi", "420")
	params.Set("language", "zh")
	params.Set("os_api", "34")
	params.Set("os_version", "16")
	params.Set("timezone", "8")
	params.Set("access", "wifi")
	params.Set("rom", "UP1A.231005.007")
	params.Set("rom_version", "UP1A.231005.007")

	req, err := http.NewRequest("POST", registerURL+"?"+params.Encode(), bytes.NewReader(body))
	if err != nil {
		return nil, err
	}
	req.Header.Set("User-Agent", userAgent)
	req.Header.Set("Content-Type", "application/json")
	client := &http.Client{Timeout: 30 * time.Second}
	resp, err := client.Do(req)
	if err != nil {
		return nil, fmt.Errorf("device_register network: %w", err)
	}
	defer resp.Body.Close()
	data, err := io.ReadAll(resp.Body)
	if err != nil {
		return nil, err
	}
	if resp.StatusCode != 200 {
		return nil, fmt.Errorf("device_register HTTP %d: %s", resp.StatusCode, strings.TrimSpace(string(data)))
	}
	var rr registerResponse
	dec := json.NewDecoder(bytes.NewReader(data))
	dec.UseNumber()
	if err := dec.Decode(&rr); err != nil {
		return nil, fmt.Errorf("device_register: bad response: %w", err)
	}
	did := rr.DeviceIDStr
	if did == "" {
		did = rr.DeviceID.String()
	}
	iid := rr.InstallIDStr
	if iid == "" {
		iid = rr.InstallID.String()
	}
	if did == "" || did == "0" {
		return nil, fmt.Errorf("device_register: no did in body: %s", strings.TrimSpace(string(data)))
	}
	return &credentials{
		Did:         did,
		InstallID:   iid,
		SSID:        newUUIDStr(),
		Cdid:        cdid,
		Openudid:    openudid,
		Clientudid:  clientudid,
		DeviceBrand: "google",
		DeviceType:  "Pixel 7 Pro",
		OSVersion:   "16",
		TS:          time.Now().UnixMilli(),
		Source:      "register",
	}, nil
}

// fetchAppKey calls the settings API and returns data.settings.asr_config.app_key.
// The app_key is stable per device; it is used as the wss URL token.
// The value itself is never logged.
func fetchAppKey(creds *credentials) (string, error) {
	now := time.Now().UnixMilli()
	params := url.Values{}
	params.Set("device_platform", "android")
	params.Set("os", "android")
	params.Set("ssmix", "a")
	params.Set("_rticket", strconv.FormatInt(now, 10))
	params.Set("cdid", creds.Cdid)
	params.Set("device_id", creds.Did)
	params.Set("aid", "401734")
	params.Set("app_name", "oime")
	params.Set("version_code", "100102018")
	params.Set("version_name", "1.1.2")
	params.Set("channel", "official")

	body := []byte("body=null")
	sum := md5.Sum(body)
	stub := strings.ToUpper(hex.EncodeToString(sum[:]))

	req, err := http.NewRequest("POST", settingsURL+"?"+params.Encode(), bytes.NewReader(body))
	if err != nil {
		return "", err
	}
	req.Header.Set("User-Agent", userAgent)
	req.Header.Set("x-ss-stub", stub)
	client := &http.Client{Timeout: 30 * time.Second}
	resp, err := client.Do(req)
	if err != nil {
		return "", fmt.Errorf("settings network: %w", err)
	}
	defer resp.Body.Close()
	data, err := io.ReadAll(resp.Body)
	if err != nil {
		return "", err
	}
	if resp.StatusCode != 200 {
		return "", fmt.Errorf("settings HTTP %d", resp.StatusCode)
	}
	var parsed struct {
		Data struct {
			Settings struct {
				AsrConfig struct {
					AppKey string `json:"app_key"`
				} `json:"asr_config"`
			} `json:"settings"`
		} `json:"data"`
	}
	if err := json.Unmarshal(data, &parsed); err != nil {
		return "", fmt.Errorf("settings: bad response: %w", err)
	}
	if parsed.Data.Settings.AsrConfig.AppKey == "" {
		return "", fmt.Errorf("settings: no app_key in response")
	}
	return parsed.Data.Settings.AsrConfig.AppKey, nil
}
