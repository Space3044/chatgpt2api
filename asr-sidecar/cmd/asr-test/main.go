// Command asr-test streams a 16kHz mono s16le PCM file through the Go ASR
// client and prints interim + final transcripts, mirroring the Node addon
// test used on the VPS.
//
// Usage:
//
//	go run ./cmd/asr-test -pcm /tmp/asr_test_zh.pcm
//	go run ./cmd/asr-test -pcm /tmp/asr_test_zh.pcm -expect "你好，这是一个语音识别测试"
//
// With -expect, the program exits non-zero unless the final transcript
// contains the expected text (punctuation/number formatting may differ).
package main

import (
	"flag"
	"fmt"
	"os"
	"strings"
	"sync"
	"time"

	"cometix-asr-go/asr"
)

func main() {
	pcmPath := flag.String("pcm", "", "16kHz mono s16le PCM file")
	expect := flag.String("expect", "", "substring the final transcript must contain (optional)")
	flag.Parse()
	if *pcmPath == "" {
		fmt.Fprintln(os.Stderr, "usage: asr-test -pcm <file.pcm> [-expect <text>]")
		os.Exit(2)
	}
	pcm, err := os.ReadFile(*pcmPath)
	if err != nil {
		fmt.Fprintln(os.Stderr, "read pcm:", err)
		os.Exit(1)
	}
	fmt.Printf("pcm: %s (%d bytes, ~%.1fs)\n", *pcmPath, len(pcm), float64(len(pcm))/32000)

	did, err := asr.EnsureDid(false)
	if err != nil {
		fmt.Fprintln(os.Stderr, "EnsureDid:", err)
		os.Exit(1)
	}
	fmt.Printf("did: %s… (cached)\n", did[:4])

	var mu sync.Mutex
	var finalText string
	var interimCount int
	done := make(chan struct{})
	var once sync.Once
	onEvent := func(ev asr.Event) {
		mu.Lock()
		defer mu.Unlock()
		switch ev.Type {
		case "ready":
			fmt.Printf("[ready] session=%s mode=%s\n", ev.SessionID, ev.Mode)
		case "transcript":
			if ev.IsFinal {
				finalText = ev.Text
				fmt.Printf("[final] %s (stage=%s)\n", ev.Text, ev.Stage)
				if ev.Stage == "session_final" {
					once.Do(func() { close(done) })
				}
			} else {
				interimCount++
				fmt.Printf("[interim] %s\n", ev.Text)
			}
		case "error":
			fmt.Printf("[error] %s %s\n", ev.Message, ev.Code)
		case "debug":
			fmt.Printf("[debug] %s\n", ev.Message)
		case "close":
			fmt.Println("[close]")
			once.Do(func() { close(done) })
		}
	}

	h, err := asr.StartSession("{}", onEvent)
	if err != nil {
		fmt.Fprintln(os.Stderr, "StartSession:", err)
		os.Exit(1)
	}
	fmt.Printf("handle=%d connected=%v\n", h, asr.IsConnected(h))

	// Stream in 1280-byte (40ms) frames, paced in real time.
	for off := 0; off < len(pcm); off += 1280 {
		end := off + 1280
		if end > len(pcm) {
			end = len(pcm)
		}
		if err := asr.FeedPCM(h, pcm[off:end]); err != nil {
			fmt.Fprintln(os.Stderr, "FeedPCM:", err)
			os.Exit(1)
		}
		time.Sleep(40 * time.Millisecond)
	}
	fmt.Println("all fed, finalizing…")
	if err := asr.FinalizeSession(h); err != nil {
		fmt.Fprintln(os.Stderr, "FinalizeSession:", err)
		os.Exit(1)
	}
	select {
	case <-done:
	case <-time.After(20 * time.Second):
		fmt.Fprintln(os.Stderr, "timed out waiting for final transcript")
	}
	asr.CloseSession(h)
	fmt.Printf("interim events: %d\nfinal: %s\n", interimCount, finalText)
	if *expect != "" {
		norm := func(s string) string {
			return strings.ReplaceAll(strings.ReplaceAll(s, "，", ""), "。", "")
		}
		if !strings.Contains(norm(finalText), norm(*expect)) {
			fmt.Fprintf(os.Stderr, "MISMATCH: final transcript does not contain %q\n", *expect)
			os.Exit(1)
		}
		fmt.Println("EXPECT OK")
	}
}
