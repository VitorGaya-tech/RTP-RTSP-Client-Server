import threading

import cv2

# Sending resolution. Each frame becomes exactly ONE RTP/UDP packet, so the JPEG must stay
# well below the ~64 KB limit of a datagram. 384x288 at quality 50 results in ~10-20 KB,
# which fits comfortably.
FRAME_SIZE = (384, 288)
JPEG_QUALITY = 50

# FPS range considered reliable. Some containers report 0, NaN or absurd values
# (e.g. 1000 or 90000 coming from the file's time base); outside this range we use
# DEFAULT_FPS so we do not compute a meaningless interval between frames.
MIN_VALID_FPS = 1
MAX_VALID_FPS = 120
DEFAULT_FPS = 20

# --- Shared webcam ----------------------------------------------------------------------
# How long each viewer waits for a new frame before returning None. Short on purpose: it is
# the ceiling on the delay for the sender thread to notice a PAUSE/TEARDOWN.
LIVE_FRAME_TIMEOUT = 0.1

# CONSECUTIVE camera read failures until the capture gives up and shuts down (~5 s). Covers
# a disconnected or frozen camera, without killing the capture over one isolated bad frame.
CAPTURE_MAX_FAILURES = 100
CAPTURE_RETRY_DELAY = 0.05

# Maximum time to wait for the capture thread to stop when the last viewer leaves.
CAPTURE_JOIN_TIMEOUT = 2.0


def _encodeFrame(frame):
    """Resize an OpenCV frame and return the JPEG bytes ready for the RTP payload."""
    # Keep the resolution small so the UDP packet does not overflow
    smaller_frame = cv2.resize(frame, FRAME_SIZE)

    # Quality 50 gives compact packets that are fast to transmit
    _, jpeg_data = cv2.imencode('.jpg', smaller_frame, [int(cv2.IMWRITE_JPEG_QUALITY), JPEG_QUALITY])
    return jpeg_data.tobytes()


class VideoStream:
    """Source of JPEG frames from a video FILE.

    Owns a native resource (cv2.VideoCapture). Whoever creates an instance must call
    release() when done with it; otherwise the file stays open until the process dies.

    Each session opens its own file, so several clients can watch the same video (or
    different videos) at the same time without getting in each other's way. The webcam,
    which is an exclusive resource, is handled separately by LiveStream.
    """

    # [FIX-2] ServerWorker needs to know whether the source is the webcam: with the webcam
    # the viewer itself blocks until a frame arrives (the clock is the camera), whereas with
    # a file the read is instantaneous and the server must pace the sending by itself.
    isLive = False

    def __init__(self, filename):
        self.filename = filename

        self.cap = cv2.VideoCapture(filename)
        # Optimization for files: reduce the disk read buffer
        self.cap.set(cv2.CAP_PROP_BUFFERSIZE, 2)

        if not self.cap.isOpened():
            # [FIX-3] Even a failed open may have allocated the file handle; release it
            # before propagating the error so it is not leaked.
            self.cap.release()
            raise IOError("Could not open the video file")

        self.frameNum = 0

        # [FIX-2] The file's FPS, used to compute the interval between frames. Previously the
        # value was read but never used, and sending ran at a fixed pace regardless of the
        # video. The chained comparison also rejects NaN (any comparison with NaN is
        # False, so it falls back to DEFAULT_FPS).
        fps = self.cap.get(cv2.CAP_PROP_FPS)
        if not (MIN_VALID_FPS <= fps <= MAX_VALID_FPS):
            fps = DEFAULT_FPS
        self.fps = fps
        self.frameInterval = 1.0 / self.fps

    def nextFrame(self):
        """Read the next frame and return its JPEG bytes.

        Returns None when there are no more frames (end of file) or after release().
        """
        # [FIX-3] After release() the cap is closed; returning None is safer than letting
        # OpenCV try to read from a handle that was already released.
        if not self.cap.isOpened():
            return None

        ret, frame = self.cap.read()
        if ret:
            self.frameNum += 1
            return _encodeFrame(frame)
        return None

    def frameNbr(self):
        return self.frameNum

    def release(self):
        """Release the file. Safe to call more than once.

        [FIX-3] It did not exist: the handle was only released when the process ended.
        """
        self.cap.release()


class _LiveCapture:
    """SINGLE webcam capture, shared by all "live" sessions.

    The camera is an exclusive resource: if each session opened its own, only the first
    would work. Here a single thread reads the camera, encodes each frame ONCE as JPEG and
    publishes the result; any number of sessions (LiveStream) consume the most recent
    frame. The camera is opened when the first viewer arrives and released when the last
    one leaves (reference counting), so it is not kept on with nobody watching.

    Security: this object only distributes unprotected JPEG bytes. Who may watch is decided
    BEFORE, in each session's SETUP, and payload encryption must be applied AFTER, per
    session and with its own key, at send time. This way sharing the capture does not
    share keys or permissions between viewers.
    """

    def __init__(self):
        # Protects the viewer count and the capture thread's lifecycle.
        self._lock = threading.Lock()
        # Protects the published frame (_latest/_seq) and wakes up whoever waits for a new frame.
        self._frameReady = threading.Condition()

        self._subscribers = 0
        self._thread = None
        self._stopEvent = threading.Event()

        self._latest = None   # JPEG bytes of the latest frame (immutable, safe to share)
        self._seq = 0         # grows with every published frame; never reset

    def acquire(self):
        """Register a viewer, opening the camera if it is the first one.

        Raises IOError if the camera cannot be opened; in that case the viewer is NOT
        counted, so there is nothing to release later.
        """
        with self._lock:
            # Also reopens when there are viewers but the capture died on its own (camera
            # unplugged): a new SETUP revives the camera instead of inheriting a dead capture.
            if self._thread is None or not self._thread.is_alive():
                self._start()
            self._subscribers += 1

    def release(self):
        """Remove a viewer, closing the camera if it was the last one."""
        with self._lock:
            if self._subscribers > 0:
                self._subscribers -= 1
            if self._subscribers == 0:
                self._stop()

    def waitFrame(self, lastSeq, timeout):
        """Wait for a frame newer than `lastSeq`. Returns (seq, jpeg), or None on timeout."""
        with self._frameReady:
            ready = self._frameReady.wait_for(
                lambda: self._latest is not None and self._seq != lastSeq, timeout)
            if not ready:
                return None
            return self._seq, self._latest

    def _start(self):
        # CAP_DSHOW is a Windows-only backend.
        cap = cv2.VideoCapture(0, cv2.CAP_DSHOW)
        cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
        if not cap.isOpened():
            cap.release()
            raise IOError("Could not open the camera")

        # Discard the frame from a previous capture: a new viewer must never receive a stale
        # image from before the camera was reopened.
        with self._frameReady:
            self._latest = None

        # A new Event per capture: an old thread that is still shutting down cannot be
        # "resurrected" by the new one's event.
        self._stopEvent = threading.Event()
        self._thread = threading.Thread(
            target=self._captureLoop, args=(cap, self._stopEvent), daemon=True, name="live-capture")
        self._thread.start()

    def _stop(self):
        """Stop the capture and wait for it to release the camera. Called with self._lock held."""
        self._stopEvent.set()
        thread, self._thread = self._thread, None
        if thread is not None:
            thread.join(timeout=CAPTURE_JOIN_TIMEOUT)
            if thread.is_alive():
                print("[LiveCapture] Warning: the capture did not stop in time")

    def _captureLoop(self, cap, stopEvent):
        """Read the camera and publish the latest frame until stopped or the camera fails.

        This thread OWNS `cap`: only it calls cap.release(). Releasing from another thread
        while read() is in progress can crash the process.
        """
        failures = 0
        try:
            while not stopEvent.is_set():
                ret, frame = cap.read()
                if not ret:
                    failures += 1
                    if failures >= CAPTURE_MAX_FAILURES:
                        print("[LiveCapture] The camera stopped delivering frames; ending the capture")
                        break
                    stopEvent.wait(CAPTURE_RETRY_DELAY)
                    continue
                failures = 0

                # Encode ONCE; every session sends these same bytes.
                jpeg = _encodeFrame(frame)
                with self._frameReady:
                    self._latest = jpeg
                    self._seq += 1
                    self._frameReady.notify_all()
        finally:
            cap.release()


# Single instance per process: there is only one webcam, so there is only one capture.
_liveCapture = _LiveCapture()


class LiveStream:
    """ONE session's view of the shared webcam capture.

    Has the same interface as VideoStream (isLive, frameInterval, nextFrame, frameNbr,
    release), so the ServerWorker treats both sources the same way. Frame numbering is per
    session (1, 2, 3...), independent of the other viewers, because it becomes the RTP
    sequence number and the client discards packets with a lower or equal number.
    """

    isLive = True

    def __init__(self):
        # May raise IOError (camera unavailable); the ServerWorker turns it into a 404.
        _liveCapture.acquire()
        self.filename = "live"
        self.frameNum = 0
        self._lastSeq = 0
        self._released = False

        # Unused for the webcam (the camera sets the pace); they exist to match the interface.
        self.fps = DEFAULT_FPS
        self.frameInterval = 1.0 / self.fps

    def nextFrame(self):
        """Return the most recent frame this session has not received yet.

        Blocks up to LIVE_FRAME_TIMEOUT for a new frame and returns None if none arrives.
        A viewer slower than the camera simply skips frames, which keeps latency low
        instead of accumulating delay.
        """
        if self._released:
            return None
        got = _liveCapture.waitFrame(self._lastSeq, LIVE_FRAME_TIMEOUT)
        if got is None:
            return None
        self._lastSeq, jpeg = got
        self.frameNum += 1
        return jpeg

    def frameNbr(self):
        return self.frameNum

    def release(self):
        """Stop watching. Safe to call more than once; the last one to leave closes the camera."""
        if self._released:
            return
        self._released = True
        _liveCapture.release()
