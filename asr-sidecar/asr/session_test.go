package asr

import (
	"encoding/json"
	"strings"
	"testing"
	"time"
)

// resultPayload builds a wire-format result_json in the shape the
// Doubao IME server sends: results[0] is the cumulative formatted
// text; a final payload also carries a per-segment nonstream_result
// entry.
func resultPayload(t *testing.T, cumulative, segment string, final bool) string {
	t.Helper()
	f := func(b bool) *bool { return &b }
	items := []resultItem{{Text: cumulative, IsInterim: f(true)}}
	if final {
		items[0].IsInterim = f(false)
		items[0].IsVadFinished = f(true)
	}
	if segment != "" {
		items = append(items, resultItem{Text: segment, IsInterim: f(false), Extra: map[string]interface{}{"nonstream_result": true}})
	}
	b, err := json.Marshal(&resultJSON{Results: items})
	if err != nil {
		t.Fatalf("marshal result payload: %v", err)
	}
	return string(b)
}

func newTestSession(t *testing.T) (*session, chan Event) {
	t.Helper()
	ch := make(chan Event, 64)
	s := &session{
		done:    make(chan struct{}),
		onEvent: func(ev Event) { ch <- ev },
	}
	return s, ch
}

// collectEvents waits for n events (emit delivers asynchronously).
func collectEvents(t *testing.T, ch chan Event, n int) []Event {
	t.Helper()
	var out []Event
	for i := 0; i < n; i++ {
		select {
		case ev := <-ch:
			out = append(out, ev)
		case <-time.After(2 * time.Second):
			t.Fatalf("timed out waiting for event %d/%d, got %+v", i+1, n, out)
		}
	}
	return out
}

func TestCumulativeTextTracksLongest(t *testing.T) {
	s, ch := newTestSession(t)
	// Interim payloads grow the cumulative text; a 1-rune revision
	// dip must not shrink it (the server revises earlier words).
	for _, cum := range []string{
		"今天天气很好",
		"今天天气很好，我们一起去公园散步吧。",
		"今天天气很好，我们一起去公园散步吧", // revision dip: 1 rune shorter
		"今天天气很好，我们一起去公园散步吧。晚上我们一起吃火锅。",
	} {
		s.handleResponse(&response{ResultJSON: resultPayload(t, cum, "", false)})
	}
	collectEvents(t, ch, 4) // interim events, not asserted here
	if got := s.cumulativeText; got != "今天天气很好，我们一起去公园散步吧。晚上我们一起吃火锅。" {
		t.Fatalf("cumulativeText: got %q", got)
	}
	// No final payload yet: nothing is settled.
	if s.settledText != "" {
		t.Fatalf("settledText without finals: got %q", s.settledText)
	}
}

func TestSettledTextTracksFinalPayloads(t *testing.T) {
	s, ch := newTestSession(t)
	s.handleResponse(&response{ResultJSON: resultPayload(t, "今天天气很好，我们一起去公园散步吧。", "今天天气很好，我们一起去公园散步吧。", true)})
	if got := s.settledText; got != "今天天气很好，我们一起去公园散步吧。" {
		t.Fatalf("settledText: got %q", got)
	}
	events := collectEvents(t, ch, 1)
	if len(events) != 1 || events[0].Stage != "stable" || events[0].StableText != "今天天气很好，我们一起去公园散步吧。" {
		t.Fatalf("stable event: got %+v", events)
	}
	// A longer final payload advances the settled text.
	s.handleResponse(&response{ResultJSON: resultPayload(t, "今天天气很好，我们一起去公园散步吧。晚上我们一起吃火锅。", "晚上我们一起吃火锅。", true)})
	if got := s.settledText; got != "今天天气很好，我们一起去公园散步吧。晚上我们一起吃火锅。" {
		t.Fatalf("settledText after second final: got %q", got)
	}
}

// TestSessionFinalIsCumulativeText is the 170s regression: the old
// merge assembled the final text from per-segment and cumulative
// finals and duplicated the passage (3120 runes for a 913-rune
// recording). The session final must be the longest cumulative
// text, even when the last final payload re-emits an early segment
// as its nonstream_result entry.
func TestSessionFinalIsCumulativeText(t *testing.T) {
	s, ch := newTestSession(t)
	first := "今天天气很好，我们一起去公园散步吧。晚上我们一起吃火锅，再聊聊最近的工作。这周末我想去爬山，顺便拍一些照片。他的普通话说得很好，几乎听不出是外国人。北京的冬天很冷，出门要穿厚厚的羽绒服。我每天早上 6 点起床，先跑步半小时再吃早饭。这部手机的电池很耐用，充一次电能用两天。"
	// Segment finals arrive with the cumulative text growing; the
	// last one re-emits the early segment (first 20 sentences) as
	// its nonstream_result entry while results[0] holds the whole
	// recording.
	second := first + "老师让我们把作业写在练习本上，明天交上来。火车站离我家不远，走路 10 分钟就到了。妈妈做的红烧肉是我最喜欢吃的一道菜。公司楼下的咖啡店环境很好，适合谈工作。我昨天看了一部电影，故事非常感人。下雨天路很滑，开车一定要注意安全。我正在学做蛋糕，昨天第一次就成功。图书馆周末人很多，最好早点去占座位。这件衣服打 5 折，现在买非常划算。爷爷每天下午都要去公园里下棋、聊天。春天来了，花园里的花都开了，非常漂亮。我每天晚上 11 点睡觉，早上 7 点起床。这台电脑的速度很快，打开软件一点都不卡。弟弟最最喜欢吃西瓜，夏天每天都要吃一块。我们学校门口有一家书店，里面有很多好书。学习英语需要每天坚持练习口语和听力。昨天晚上下了一场大雨，今天空气很清新。奶奶做的饺子特别好吃，我一口气吃了 20 个。坐高铁去上海只要 4 个小时，比开车快多了。这件毛衣是妈妈亲手织的，"
	third := second + "这家店的服务态度很好，老板也很热情。明天是妈妈的生日，我准备给她做一个蛋糕。河边的柳树发芽了，春天真的来了。我正在减肥，晚饭只吃蔬菜和一点米饭。这部电视剧一共有 40 集，我已经看到第 10 集了。明天早上 8 点开会，请大家准时参加。我买了一本新书，讲的是中国历史故事。坐地铁去市中心大概需要 40 分钟时间。孩子们在操场上踢足球，玩得非常开心。请把这份文件打印 3 份，下午交给我。"
	earlyRunes := []rune(first)
	early := string(earlyRunes[:100]) // re-emitted early segment
	s.handleResponse(&response{ResultJSON: resultPayload(t, first, first, true)})
	s.handleResponse(&response{ResultJSON: resultPayload(t, second, second, true)})
	s.handleResponse(&response{ResultJSON: resultPayload(t, third, early, true)})
	s.handleResponse(&response{MessageType: "SessionFinished"})
	if got := s.cumulativeText; got != third {
		t.Fatalf("cumulativeText: got %d runes, want %d", len([]rune(got)), len([]rune(third)))
	}
	// 3 stable events + session_final + close.
	events := collectEvents(t, ch, 5)
	var finalEv *Event
	for i := range events {
		if events[i].Stage == "session_final" {
			finalEv = &events[i]
		}
	}
	if finalEv == nil {
		t.Fatalf("no session_final event in %+v", events)
	}
	if finalEv.Text != third || finalEv.StableText != third {
		t.Fatalf("session_final text: got %d runes, want %d", len([]rune(finalEv.Text)), len([]rune(third)))
	}
	if strings.Count(finalEv.Text, "今天天气很好") != 1 {
		t.Fatalf("session_final text repeats the opening")
	}
}

func TestSessionFinalEmptyWithoutResults(t *testing.T) {
	s, ch := newTestSession(t)
	s.handleResponse(&response{MessageType: "SessionFinished"})
	// Only the close event arrives (no session_final without results).
	events := collectEvents(t, ch, 1)
	for _, ev := range events {
		if ev.Stage == "session_final" {
			t.Fatalf("session_final emitted without results: %+v", ev)
		}
	}
}
