// Command postasr-test calls the Go RunPostAsr (fmt + NER) and prints
// the result JSON, mirroring the Node addon's runPostAsr test used on
// the VPS.
//
// Usage:
//
//	go run ./cmd/postasr-test -text "你好这是一个语音识别测试"
//	go run ./cmd/postasr-test -config '{"did":"...","enableFmt":true,"enableNer":true}' -text "..."
//
// With no -config, the config is built from -did (default: the cached
// did from EnsureDid), -enable-fmt (default true), -enable-ner
// (default true), -sami-token (default: omitted, so get_config runs)
// and -uid. The program prints the result JSON; it exits non-zero only
// on a config error, not on fmt/ner step failures (those are in the
// result's steps/errors, as in the Rust addon).
package main

import (
	"encoding/json"
	"flag"
	"fmt"
	"os"

	"cometix-asr-go/asr"
)

func main() {
	config := flag.String("config", "", "PostAsrConfig JSON (did, enableFmt, enableNer, samiToken, uid)")
	text := flag.String("text", "", "already-recognised text to post-process")
	did := flag.String("did", "", "device id (default: cached did)")
	enableFmt := flag.Bool("enable-fmt", true, "enable fmt")
	enableNer := flag.Bool("enable-ner", true, "enable NER")
	samiToken := flag.String("sami-token", "", "samiToken; empty means omitted (get_config runs)")
	uid := flag.String("uid", "", "uid")
	flag.Parse()
	if *text == "" {
		fmt.Fprintln(os.Stderr, "usage: postasr-test -text <text> [-config <json>] [-did <did>]")
		os.Exit(2)
	}
	cfgJSON := *config
	if cfgJSON == "" {
		d := *did
		if d == "" {
			var err error
			d, err = asr.EnsureDid(false)
			if err != nil {
				fmt.Fprintln(os.Stderr, "EnsureDid:", err)
				os.Exit(1)
			}
		}
		cfg := map[string]interface{}{
			"did":       d,
			"enableFmt": *enableFmt,
			"enableNer": *enableNer,
			"uid":       *uid,
		}
		if *samiToken != "" {
			cfg["samiToken"] = *samiToken
		}
		b, err := json.Marshal(cfg)
		if err != nil {
			fmt.Fprintln(os.Stderr, "marshal config:", err)
			os.Exit(1)
		}
		cfgJSON = string(b)
	}
	out, err := asr.RunPostAsr(cfgJSON, *text)
	if err != nil {
		fmt.Fprintln(os.Stderr, "RunPostAsr:", err)
		os.Exit(1)
	}
	var pretty interface{}
	if err := json.Unmarshal([]byte(out), &pretty); err == nil {
		b, _ := json.MarshalIndent(pretty, "", "  ")
		fmt.Println(string(b))
		return
	}
	fmt.Println(out)
}
