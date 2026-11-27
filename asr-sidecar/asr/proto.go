package asr

import (
	"encoding/json"
	"fmt"
	"strings"
)

// Handwritten protobuf codec for AsrRequest / AsrResponse (see asr.proto).
// Field numbers verified against frames captured from the Rust addon.

func appendVarint(b []byte, v uint64) []byte {
	for {
		if v < 0x80 {
			return append(b, byte(v))
		}
		b = append(b, byte(v)|0x80)
		v >>= 7
	}
}

func appendTag(b []byte, field int, wire int) []byte {
	return appendVarint(b, uint64(field<<3|wire))
}

func appendStringField(b []byte, field int, s string) []byte {
	if s == "" {
		return b
	}
	b = appendTag(b, field, 2)
	b = appendVarint(b, uint64(len(s)))
	return append(b, s...)
}

func appendBytesField(b []byte, field int, data []byte) []byte {
	if len(data) == 0 {
		return b
	}
	b = appendTag(b, field, 2)
	b = appendVarint(b, uint64(len(data)))
	return append(b, data...)
}

func appendVarintField(b []byte, field int, v int32) []byte {
	if v == 0 {
		return b
	}
	b = appendTag(b, field, 0)
	return appendVarint(b, uint64(uint32(v)))
}

// marshalRequest builds an AsrRequest. token is omitted for TaskRequest
// (verified in the capture: TaskRequest has no field 2).
func marshalRequest(token, method, payload string, audio []byte, requestID string, frameNo int32) []byte {
	var b []byte
	b = appendStringField(b, 2, token)
	b = appendStringField(b, 3, "ASR")
	b = appendStringField(b, 5, method)
	b = appendStringField(b, 6, payload)
	b = appendBytesField(b, 7, audio)
	b = appendStringField(b, 8, requestID)
	b = appendVarintField(b, 9, frameNo)
	return b
}

// response is a decoded AsrResponse. Unknown fields are ignored.
type response struct {
	RequestID     string
	TaskID        string
	ServiceName   string
	MessageType   string
	StatusCode    int32
	StatusMessage string
	ResultJSON    string
}

func parseResponse(data []byte) (*response, error) {
	r := &response{}
	i := 0
	for i < len(data) {
		tag, n, err := readVarint(data, i)
		if err != nil {
			return nil, err
		}
		i = n
		field := int(tag >> 3)
		wire := int(tag & 7)
		switch wire {
		case 0:
			v, n, err := readVarint(data, i)
			if err != nil {
				return nil, err
			}
			i = n
			if field == 5 {
				r.StatusCode = int32(v)
			}
			// other varint fields ignored
		case 2:
			ln, n, err := readVarint(data, i)
			if err != nil {
				return nil, err
			}
			i = n
			if i+int(ln) > len(data) {
				return nil, fmt.Errorf("protobuf: truncated field %d", field)
			}
			s := string(data[i : i+int(ln)])
			i += int(ln)
			switch field {
			case 1:
				r.RequestID = s
			case 2:
				r.TaskID = s
			case 3:
				r.ServiceName = s
			case 4:
				r.MessageType = s
			case 6:
				r.StatusMessage = s
			case 7:
				r.ResultJSON = s
			}
		default:
			return nil, fmt.Errorf("protobuf: unsupported wire type %d", wire)
		}
	}
	return r, nil
}

func readVarint(data []byte, i int) (uint64, int, error) {
	var v uint64
	var shift uint
	for {
		if i >= len(data) {
			return 0, 0, fmt.Errorf("protobuf: truncated varint")
		}
		b := data[i]
		i++
		v |= uint64(b&0x7f) << shift
		if b&0x80 == 0 {
			return v, i, nil
		}
		shift += 7
		if shift > 63 {
			return 0, 0, fmt.Errorf("protobuf: varint too long")
		}
	}
}

// resultJSON is the parsed form of AsrResponse.result_json.
//
// Real wire shape (captured live):
//   - heartbeat: {"results":null,"extra":{"packet_number":N,"heartbeat_num":N}}
//   - VAD start: {"results":[{"text":"","is_interim":true}],"extra":{"vad_start":true}}
//   - interim:   {"results":[
//     {"text":"<formatted>","is_interim":true},
//     {"text":"<raw>","is_interim":true,"stream_asr_finish":false,"extra":{"seq_id":N}}],
//     "extra":{"seq_id":N}}
//   - final:     {"results":[
//     {"text":"<formatted>","is_interim":false,"is_vad_finished":true},
//     {"text":"<formatted>","is_interim":false,"extra":{"nonstream_result":true}}],
//     "extra":{...latencies...}}
//
// results[0] is the formatted (punctuated) text; results[1] is the raw
// unpunctuated text. Only results[0] is used for display/final text.
type resultJSON struct {
	Results []resultItem           `json:"results"`
	Extra   map[string]interface{} `json:"extra"`
}

type resultItem struct {
	Text            string                 `json:"text"`
	IsInterim       *bool                  `json:"is_interim"`
	IsVadFinished   *bool                  `json:"is_vad_finished"`
	StreamAsrFinish bool                   `json:"stream_asr_finish"`
	Extra           map[string]interface{} `json:"extra"`
}

// text returns the formatted text (results[0]).
func (r *resultJSON) text() string {
	if len(r.Results) == 0 {
		return ""
	}
	return r.Results[0].Text
}

// isFinal reports whether this payload is a final (VAD-finished) result.
func (r *resultJSON) isFinal() bool {
	for _, it := range r.Results {
		if it.IsInterim != nil && !*it.IsInterim {
			if it.IsVadFinished != nil && *it.IsVadFinished {
				return true
			}
		}
		if it.Extra != nil {
			if _, ok := it.Extra["nonstream_result"]; ok {
				return true
			}
		}
	}
	return false
}

func parseResultJSON(s string) (*resultJSON, error) {
	var r resultJSON
	dec := json.NewDecoder(strings.NewReader(s))
	if err := dec.Decode(&r); err != nil {
		return nil, err
	}
	return &r, nil
}
