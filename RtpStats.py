import threading
import time
from collections import deque

# Window used for the "instantaneous" values (the ones in the line printed every second).
WINDOW_SECONDS = 1.0

# A gap longer than this between two consecutive packets is treated as a PAUSE (or end of
# video, or a long stall) and does NOT count as playback time. Without this, someone who
# pauses for 30 s would have the average FPS and rate divided by the stopped time. The
# 1 fps minimum accepted by VideoStream gives at most 1 s between frames, so 2 s leaves margin.
PAUSE_GAP_SECONDS = 2.0

# The RTP sequence number has 16 bits and wraps back to zero after 65535.
SEQ_MODULO = 0x10000

# Sequence number arithmetic (RFC 1982): the distance (new - last) modulo 65536 up to half
# of the space means "ahead"; above that, the packet is BEFORE the last one (arrived late
# or out of order). This works across the wrap-around.
HALF_SEQ_SPACE = 0x8000


def formatBitrate(bytesPerSecond):
    """Convert bytes/s into human-readable bits/s (decimal units, as used in networking)."""
    bps = bytesPerSecond * 8
    if bps >= 1e6:
        return "%.2f Mbps" % (bps / 1e6)
    if bps >= 1e3:
        return "%.0f kbps" % (bps / 1e3)
    return "%.0f bps" % bps


class RtpStats:
    """RTP reception statistics: packet loss, frames per second and data rate.

    Replaces the old calculation in Client, which had several errors:
      - it measured the size of the Python OBJECT (sys.getsizeof), not the bytes received;
      - it divided the size of ONE packet by the total time, which is not a rate;
      - it did "(old + new) / 2" on every packet, which is not an average but a filter
        that forgets almost all of the history;
      - it applied that same filter on top of a loss that was already cumulative.

    Here each quantity is computed once, in the standard way:
      - loss     = (expected - received) / expected, with "expected" coming from the
                   sequence numbers, as in RTCP (RFC 3550, section 6.4.1);
      - FPS/rate = quantity (frames/bytes) divided by the time the video was actually
                   arriving.

    Each frame travels in ONE RTP packet in this project, so frames/s == packets/s.

    Thread safety: update() is called by the RTP receive thread and snapshot() by the
    interface thread, so everything goes through a lock.
    """

    def __init__(self, clock=time.monotonic):
        # Injectable clock: allows testing with synthetic times, without depending on sleep.
        self._clock = clock
        self._lock = threading.Lock()

        # --- sequence / loss ---
        self._baseSeq = None   # first sequence number seen
        self._extMax = 0       # highest "extended" sequence number (no wrap) seen
        self._received = 0     # packets received (not counting exact duplicates)
        self._late = 0         # packets that arrived late or out of order
        self._duplicates = 0   # packets repeating the last sequence number

        # --- time / bytes ---
        self._totalBytes = 0       # bytes of all received packets (RTP header included)
        self._lastArrival = None   # instant of the last packet
        self._activeTime = 0.0     # sum of the intervals between packets, without pauses
        self._intervals = 0        # how many intervals went into _activeTime
        self._intervalBytes = 0    # bytes of the packets that closed each interval

        # (instant, bytes) of the packets in the last window, for the instantaneous values.
        self._window = deque()

    def update(self, seq, nbytes, now=None):
        """Record a received packet and return its EXTENDED sequence number.

        `seq` is the 16-bit number from the header and `nbytes` the size of the received
        packet. The returned value keeps growing after the wrap (65535 -> 65536 instead of
        0), so the caller can compare "is this packet newer than the last one shown?"
        without worrying about the wrap to zero. For a late packet, it returns a value
        SMALLER than the highest one already seen.
        """
        if now is None:
            now = self._clock()

        with self._lock:
            # --- time and bytes ---
            # Each packet closes an interval with the previous one; the interval and the
            # bytes of the packet that closed it go together, so that rate = bytes / time
            # is consistent. The first packet (or the first after a pause) only opens an
            # interval.
            if self._lastArrival is not None:
                gap = now - self._lastArrival
                if gap <= PAUSE_GAP_SECONDS:
                    self._activeTime += gap
                    self._intervals += 1
                    self._intervalBytes += nbytes
            self._lastArrival = now
            self._totalBytes += nbytes

            self._window.append((now, nbytes))
            self._pruneWindow(now)

            # --- sequence ---
            if self._baseSeq is None:
                self._baseSeq = seq
                self._extMax = seq
                self._received = 1
                return seq

            # Since _extMax mod 65536 is the last 16-bit number seen, this difference is
            # the correct distance even across the wrap-around.
            delta = (seq - self._extMax) & (SEQ_MODULO - 1)

            if delta == 0:
                # Same number as the last packet: duplicate. Does not count as received.
                self._duplicates += 1
                return self._extMax

            if delta < HALF_SEQ_SPACE:
                # Packet ahead. If delta > 1, the numbers in between have not arrived yet
                # (lost, or just late); they enter the loss count by difference.
                self._extMax += delta
                self._received += 1
                return self._extMax

            # Packet before the last one: it arrived late (or out of order). It fills a hole
            # opened earlier, so it counts as received and reduces the computed loss.
            self._late += 1
            self._received += 1
            return self._extMax - (SEQ_MODULO - delta)

    def snapshot(self, now=None):
        """Return a consistent copy of the current statistics, as a dictionary.

        Keys:
          received, expected, lost, late, duplicates  -> packet counts (== frames)
          lossPercent   -> cumulative loss in %
          totalBytes    -> bytes received
          playTime      -> seconds of playback (without pauses)
          avgFps        -> average frames/s over the playback time
          avgBitrate    -> average bytes/s over the playback time
          fps, bitrate  -> the same, but only over the last window (~1 s)
        """
        if now is None:
            now = self._clock()

        with self._lock:
            if self._baseSeq is None:
                expected = 0
            else:
                expected = self._extMax - self._baseSeq + 1
            # It can go negative if there are duplicates or packets that arrived too late;
            # the loss is never less than zero.
            lost = max(0, expected - self._received)

            self._pruneWindow(now)
            fps, bitrate = self._windowRates()

            return {
                'received': self._received,
                'expected': expected,
                'lost': lost,
                'late': self._late,
                'duplicates': self._duplicates,
                'lossPercent': (lost / expected * 100.0) if expected else 0.0,
                'totalBytes': self._totalBytes,
                'playTime': self._activeTime,
                'avgFps': (self._intervals / self._activeTime) if self._activeTime > 0 else 0.0,
                'avgBitrate': (self._intervalBytes / self._activeTime) if self._activeTime > 0 else 0.0,
                'fps': fps,
                'bitrate': bitrate,
            }

    def summaryLine(self):
        """A short line with the values of the last window, suited to periodic logging."""
        s = self.snapshot()
        return "[RTP] %.1f fps | %s | loss %.2f%% (%d/%d)" % (
            s['fps'], formatBitrate(s['bitrate']), s['lossPercent'], s['lost'], s['expected'])

    def _pruneWindow(self, now):
        """Drop from the window the packets older than WINDOW_SECONDS. Call with the lock held."""
        limit = now - WINDOW_SECONDS
        while self._window and self._window[0][0] < limit:
            self._window.popleft()

    def _windowRates(self):
        """FPS and bytes/s measured between the first and last packet in the window. With the lock."""
        if len(self._window) < 2:
            return 0.0, 0.0
        first, last = self._window[0][0], self._window[-1][0]
        span = last - first
        if span <= 0:
            return 0.0, 0.0
        # N packets form N-1 intervals; the bytes of the first packet are not part of the
        # calculation, for the same reason as the average in update().
        intervalBytes = sum(size for _, size in list(self._window)[1:])
        return (len(self._window) - 1) / span, intervalBytes / span
