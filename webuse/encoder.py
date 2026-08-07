"""Hardware H.264/HEVC encoder: JPEG frames in, Annex-B access units out.

Pipes captured JPEGs (e.g. WindowCapture.jpegs()) into ffmpeg's VideoToolbox
encoder and reads back an Annex-B elementary stream, which we split into ACCESS
UNITS classified KEY (contains an IDR/IRAP) vs DELTA. That framing is exactly
what a browser WebCodecs VideoDecoder wants: one EncodedVideoChunk per AU.

Design mirrors capture.jpegs() (async, run blocking IO in the executor) and
tunnel.py's care about not leaving a zombie subprocess: stop() / generator
teardown terminate ffmpeg and close every pipe.

    enc = Encoder(codec="h264", fps=15)
    async for au in enc.access_units(cap.jpegs()):
        au["type"]  # "key" | "delta"
        au["data"]  # bytes, Annex-B (start codes included)
"""

import asyncio
import os
import shutil
import subprocess
import threading

FFMPEG = shutil.which("ffmpeg") or "/opt/homebrew/bin/ffmpeg"


def _codec_args(codec: str) -> tuple[str, str, list[str]]:
    """(videotoolbox encoder, output format, extra flags) for a codec name."""
    if codec == "hevc":
        return "hevc_videotoolbox", "hevc", ["-tag:v", "hvc1"]
    if codec == "h264":
        return "h264_videotoolbox", "h264", []
    raise ValueError(f"unknown codec {codec!r}")


class _AnnexBSplitter:
    """Incrementally splits an Annex-B byte stream into access units.

    Feed raw stdout chunks; get back {type, data} dicts. An AU boundary is
    detected before any NAL that can start a new access unit (params/AUD/SEI, or
    the first VCL NAL of a new picture) *once the current AU already holds a VCL
    NAL* — leading parameter sets stay attached to the picture that follows them,
    so a keyframe's SPS/PPS(/VPS) travel inside its KEY unit.
    """

    def __init__(self, codec: str):
        self.codec = codec  # "h264" | "hevc"
        self.buf = bytearray()      # unparsed tail (starts at a start code)
        self.cur = bytearray()      # current AU, raw bytes incl. start codes
        self.cur_has_vcl = False
        self.cur_is_key = False
        self.sps: bytes | None = None  # latest param-set NALs (no start code)
        self.pps: bytes | None = None
        self.vps: bytes | None = None

    # --- per-codec NAL classification --------------------------------------
    def _parse(self, nal: bytes) -> tuple[int, bool]:
        """Return (nal_type, starts_new_access_unit) for a NAL beginning with a
        3-byte start code."""
        if self.codec == "h264":
            hdr = nal[3]
            ntype = hdr & 0x1F
            if ntype in (6, 7, 8, 9):            # SEI / SPS / PPS / AUD
                return ntype, True
            if 1 <= ntype <= 5:                   # VCL
                nxt = nal[4] if len(nal) > 4 else 0
                return ntype, bool(nxt & 0x80)   # first_mb_in_slice == 0 -> new pic
            return ntype, False
        # HEVC: 2-byte NAL header, type = bits 1..6 of the first byte.
        ntype = (nal[3] >> 1) & 0x3F
        if ntype >= 32:                           # VPS/SPS/PPS/AUD/SEI/...
            return ntype, True
        nxt = nal[5] if len(nal) > 5 else 0       # first byte of slice header
        return ntype, bool(nxt & 0x80)            # first_slice_segment_in_pic_flag

    def _is_vcl(self, ntype: int) -> bool:
        return ntype <= 31 if self.codec == "hevc" else 1 <= ntype <= 5

    def _is_key(self, ntype: int) -> bool:
        return 16 <= ntype <= 23 if self.codec == "hevc" else ntype == 5

    def _note_param(self, ntype: int, nal: bytes):
        body = bytes(nal[3:])  # strip the 3-byte start code
        if self.codec == "h264":
            if ntype == 7:
                self.sps = body
            elif ntype == 8:
                self.pps = body
        else:
            if ntype == 32:
                self.vps = body
            elif ntype == 33:
                self.sps = body
            elif ntype == 34:
                self.pps = body

    # --- stream driving -----------------------------------------------------
    def _emit(self) -> dict:
        au = {"type": "key" if self.cur_is_key else "delta", "data": bytes(self.cur)}
        self.cur = bytearray()
        self.cur_has_vcl = False
        self.cur_is_key = False
        return au

    def _add_nal(self, nal: bytes):
        ntype, starts = self._parse(nal)
        if starts and self.cur_has_vcl:
            yield self._emit()
        self.cur += nal
        if self._is_vcl(ntype):
            self.cur_has_vcl = True
        if self._is_key(ntype):
            self.cur_is_key = True
        self._note_param(ntype, nal)

    def feed(self, chunk: bytes):
        self.buf += chunk
        b = self.buf
        pos = []
        i = b.find(b"\x00\x00\x01", 0)
        while i != -1:
            pos.append(i)
            i = b.find(b"\x00\x00\x01", i + 3)
        # Every NAL except the last is fully delimited (its end == next code).
        for k in range(len(pos) - 1):
            yield from self._add_nal(bytes(b[pos[k]:pos[k + 1]]))
        if pos:
            self.buf = bytearray(b[pos[-1]:])  # retain the still-open last NAL

    def flush(self):
        if len(self.buf) >= 4 and bytes(self.buf[:3]) == b"\x00\x00\x01":
            yield from self._add_nal(bytes(self.buf))
            self.buf = bytearray()
        if self.cur_has_vcl:
            yield self._emit()


class Encoder:
    """Spawns ffmpeg (VideoToolbox) and turns a JPEG async-iterable into a stream
    of Annex-B access units. One instance == one ffmpeg process == one stream, so
    every stream naturally begins on an IDR."""

    def __init__(
        self,
        codec: str = "h264",
        fps: float = 15.0,
        keyint: int | None = None,
        bitrate: str | None = None,
        ffmpeg: str = FFMPEG,
    ):
        if codec not in ("h264", "hevc"):
            raise ValueError(f"unknown codec {codec!r}")
        self.codec = codec
        self.fps = fps
        self.keyint = keyint if keyint is not None else max(2, int(2 * fps))
        self.bitrate = bitrate
        self.ffmpeg = ffmpeg
        self._proc: subprocess.Popen | None = None
        self._stop = threading.Event()
        self._splitter = _AnnexBSplitter(codec)

    @property
    def sps(self) -> bytes | None:
        return self._splitter.sps

    @property
    def pps(self) -> bytes | None:
        return self._splitter.pps

    @property
    def vps(self) -> bytes | None:
        return self._splitter.vps

    def codec_string(self) -> str:
        """WebCodecs codec string. For H.264 derived from the SPS once seen
        (avc1.PPCCLL); a sensible default until then / for HEVC."""
        if self.codec == "h264":
            s = self._splitter.sps
            if s and len(s) >= 4:
                return "avc1.%02x%02x%02x" % (s[1], s[2], s[3])
            return "avc1.640028"
        return "hev1.1.6.L93.B0"

    def _argv(self) -> list[str]:
        enc, fmt, extra = _codec_args(self.codec)
        rate = ["-b:v", self.bitrate] if self.bitrate else []
        return [
            self.ffmpeg, "-hide_banner", "-loglevel", "error",
            "-f", "mjpeg", "-framerate", str(self.fps), "-i", "-",
            "-c:v", enc, "-realtime", "1", "-g", str(self.keyint), "-bf", "0",
            *rate, *extra, "-f", fmt, "-",
        ]

    def _write(self, proc: subprocess.Popen, jpg: bytes):
        proc.stdin.write(jpg)
        proc.stdin.flush()

    async def access_units(self, jpeg_aiter):
        """Async generator over {type, data} access units for the given JPEG
        async-iterable. Cleans up ffmpeg + threads on exhaustion / aclose()."""
        loop = asyncio.get_running_loop()
        proc = subprocess.Popen(
            self._argv(), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
        self._proc = proc

        async def _feed():
            try:
                async for jpg in jpeg_aiter:
                    if self._stop.is_set():
                        break
                    try:
                        await loop.run_in_executor(None, self._write, proc, jpg)
                    except (BrokenPipeError, ValueError, OSError):
                        break
            finally:
                try:
                    proc.stdin.close()
                except Exception:
                    pass

        feeder = asyncio.create_task(_feed())
        fd = proc.stdout.fileno()
        try:
            while not self._stop.is_set():
                chunk = await loop.run_in_executor(None, os.read, fd, 1 << 16)
                if not chunk:
                    break
                for au in self._splitter.feed(chunk):
                    yield au
            for au in self._splitter.flush():
                yield au
        finally:
            feeder.cancel()
            try:
                await _aclose_source(jpeg_aiter)
            except Exception:
                pass
            self.stop()

    def stop(self):
        """Terminate ffmpeg and close its pipes (idempotent). Mirrors tunnel.py:
        terminate, wait briefly, then kill."""
        self._stop.set()
        proc = self._proc
        if proc is None:
            return
        for pipe in (proc.stdin, proc.stdout):
            try:
                pipe.close()
            except Exception:
                pass
        if proc.poll() is None:
            try:
                proc.terminate()
                proc.wait(timeout=5)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass


async def _aclose_source(aiter):
    """aclose() the source async-iterable if it supports it, so we stop pulling
    capture frames the moment the encoder tears down."""
    aclose = getattr(aiter, "aclose", None)
    if aclose is not None:
        await aclose()
