package asr

/*
#cgo pkg-config: opus
#include <opus/opus.h>
#include <stdlib.h>

// cgo cannot call the variadic opus_encoder_ctl directly, so wrap the
// two ctl requests we need.
static int opus_ctl_bitrate(OpusEncoder *enc, opus_int32 v) {
  return opus_encoder_ctl(enc, OPUS_SET_BITRATE_REQUEST, v);
}
static int opus_ctl_vbr(OpusEncoder *enc, opus_int32 v) {
  return opus_encoder_ctl(enc, OPUS_SET_VBR_REQUEST, v);
}
*/
import "C"

import (
	"errors"
	"unsafe"
)

// opusEncoder wraps libopus via cgo. One encoder handles 16kHz mono,
// 640-sample (40ms) frames at a fixed 64kbps CBR — exactly the frames the
// Rust addon sends (320 bytes per frame, verified in the capture).
type opusEncoder struct {
	enc *C.OpusEncoder
}

func newOpusEncoder(sampleRate, channels int) (*opusEncoder, error) {
	var err C.int
	enc := C.opus_encoder_create(C.opus_int32(sampleRate), C.int(channels), C.OPUS_APPLICATION_VOIP, &err)
	if err != C.OPUS_OK {
		return nil, errors.New("opus_encoder_create failed")
	}
	e := &opusEncoder{enc: enc}
	if rc := C.opus_ctl_bitrate(enc, C.opus_int32(64000)); rc != C.OPUS_OK {
		e.Close()
		return nil, errors.New("opus set bitrate failed")
	}
	if rc := C.opus_ctl_vbr(enc, C.opus_int32(0)); rc != C.OPUS_OK {
		e.Close()
		return nil, errors.New("opus set CBR failed")
	}
	return e, nil
}

// Encode encodes exactly 640 int16 samples (1280 bytes, s16le) into one
// Opus packet. The caller must pass full frames.
func (e *opusEncoder) Encode(pcm []byte) ([]byte, error) {
	if len(pcm) != 1280 {
		return nil, errors.New("opus: frame must be 1280 bytes (640 s16le samples)")
	}
	out := make([]byte, 4000)
	n := C.opus_encode(e.enc, (*C.opus_int16)(unsafe.Pointer(&pcm[0])), C.int(640), (*C.uchar)(unsafe.Pointer(&out[0])), C.opus_int32(len(out)))
	if n < 0 {
		return nil, errors.New("opus_encode failed")
	}
	return out[:int(n)], nil
}

func (e *opusEncoder) Close() {
	if e.enc != nil {
		C.opus_encoder_destroy(e.enc)
		e.enc = nil
	}
}
