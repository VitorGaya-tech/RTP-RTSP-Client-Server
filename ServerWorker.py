from random import randint
import sys, traceback, threading, socket, os
import time

from VideoStream import VideoStream, LiveStream
from RtpPacket import RtpPacket

# How many CONSECUTIVE failed webcam reads we tolerate before declaring the source dead.
# DSHOW often returns empty frames while the camera warms up, so a single failure must not
# bring the stream down. For a file there is no tolerance: the first failure is the end of the video.
LIVE_MAX_READ_FAILURES = 30

# Pause between attempts after a failed read, to avoid spinning in a tight loop.
READ_RETRY_DELAY = 0.05

# How many consecutive UDP send errors we tolerate before giving up on sending.
MAX_SEND_FAILURES = 20

# Maximum time to wait for the sender thread to stop (PAUSE/TEARDOWN/disconnect).
SENDER_JOIN_TIMEOUT = 2.0


class ServerWorker:
	"""Serves ONE RTSP session: receives commands over TCP and streams RTP over UDP.

	Two threads per session:
	  - "rtsp-worker": control loop (SETUP/PLAY/PAUSE/TEARDOWN) in recvRtspRequest;
	  - "rtp-sender":  created on every PLAY and stopped on every PAUSE/TEARDOWN, in sendRtp.

	Every allocated resource (sender thread, UDP socket, camera/file, TCP socket) is
	released in _releaseResources(), which always runs when the session ends, whether by
	TEARDOWN, a dropped connection or an unexpected error.
	"""

	SETUP = 'SETUP'
	PLAY = 'PLAY'
	PAUSE = 'PAUSE'
	TEARDOWN = 'TEARDOWN'

	INIT = 0
	READY = 1
	PLAYING = 2
	state = INIT

	OK_200 = 0
	FILE_NOT_FOUND_404 = 1
	CON_ERR_500 = 2

	clientInfo = {}

	def __init__(self, clientInfo, onClose=None):
		"""`onClose(worker)`, if given, is called once when the session ends.

		[FIX-4] The Server uses the callback to free the session's slot: with a single
		server and several clients, it needs to know when each session is over.
		"""
		self.clientInfo = clientInfo
		self.onClose = onClose
		# [FIX-1] Set when TEARDOWN is handled; makes the receive loop finish.
		self.teardownDone = False
		# Identifies the client in logs and thread names: with several simultaneous
		# sessions, messages without an origin are impossible to read.
		ip, port = self.clientInfo['rtspSocket'][1][:2]
		self.tag = f"{ip}:{port}"

	def run(self):
		# [FIX-4] daemon=True: the session thread must not keep the process (uvicorn)
		# from exiting if a session is still active.
		threading.Thread(
			target=self.recvRtspRequest, daemon=True,
			name=f"rtsp-worker-{self.tag}").start()

	def shutdown(self):
		"""Ask for the session to end from ANOTHER thread (e.g. Server.stop()).

		Shutting down the TCP socket wakes the blocked recv() in recvRtspRequest, which then
		follows the normal termination path and releases every resource.
		"""
		try:
			self.clientInfo['rtspSocket'][0].shutdown(socket.SHUT_RDWR)
		except OSError:
			pass  # was already closed

	def recvRtspRequest(self):
		"""Session control loop: receives and dispatches RTSP requests.

		The session ends in one of these cases, and in all of them the `finally` releases
		the resources:
		  1. the client sends TEARDOWN;
		  2. the client closes or drops the TCP connection;
		  3. an unexpected error occurs while processing a request.
		"""
		connSocket = self.clientInfo['rtspSocket'][0]
		try:
			while not self.teardownDone:
				try:
					data = connSocket.recv(256)
				except OSError:
					# Connection reset/aborted by the client: treat it as a disconnect.
					break

				if not data:
					# [FIX-1] recv() returns b'' when the other side closed the connection.
					# On a closed socket it returns IMMEDIATELY, so without this break the
					# original `while True` spun at 100% CPU forever.
					break

				request = data.decode("utf-8")
				print("Data received:\n" + request)
				self.processRtspRequest(request)
		except Exception:
			# A malformed request or an internal error ends the session cleanly (the
			# finally releases everything), instead of letting the thread die with
			# resources still allocated.
			print(f"[ServerWorker {self.tag}] Unexpected error in the session; closing it.")
			traceback.print_exc(file=sys.stdout)
		finally:
			self._releaseResources()

	def processRtspRequest(self, data):
		"""Process RTSP request sent from the client."""
		# Split the request into lines
		request = data.split('\n')

		# Take the whole first line (e.g. "SETUP Video Project 1.mp4 RTSP/1.0")
		first_line = request[0]

		# Split the line on spaces
		parts = first_line.split(' ')

		# The request type (SETUP, PLAY) is always the first word
		requestType = parts[0]

		# The file name is EVERYTHING between the initial command and the final RTSP/1.0.
		# We join the middle words with a space again.
		filename = " ".join(parts[1:-1])

		# Get the RTSP sequence number (from the second line)
		seq = request[1].split(' ')

		# Process SETUP request
		if requestType == self.SETUP:
			if self.state == self.INIT:
				print("processing SETUP\n")

				try:
					if filename == 'live':
						# Webcam: all sessions share ONE capture (see LiveStream).
						self.clientInfo['videoStream'] = LiveStream()
					# Prepend the "videos/" folder to the file name
					else:
						# Now that the server is reachable over the network, the name comes
						# from outside and must NOT be a path: "../x", "/etc/x" or "C:\\x"
						# would escape the videos/ folder. We only accept a plain name;
						# anything else is "not found".
						if (not filename or filename in ('.', '..')
								or filename != os.path.basename(filename)
								or '\\' in filename or ':' in filename):
							self.replyRtsp(self.FILE_NOT_FOUND_404, seq[1])
							return
						video_path = f"videos/{filename}"
						self.clientInfo['videoStream'] = VideoStream(video_path)
				except IOError:
					# [FIX-5] Previously, the `except` only printed and the code carried on:
					# it created a session and answered "200 OK" for a video that does not
					# exist, leaving the client "ready" to play something that is not there.
					# Now we really answer 404 and stay in INIT, so the client can try
					# another SETUP on the same connection.
					self.replyRtsp(self.FILE_NOT_FOUND_404, seq[1])
					return

				# Get the RTP/UDP port from the last line. Read BEFORE replying: if the
				# request is malformed, we fail without having promised 200 to the client.
				self.clientInfo['rtpPort'] = request[2].split(' ')[3]

				# Generate a randomized RTSP session ID
				self.clientInfo['session'] = randint(100000, 999999)

				self.state = self.READY

				# Send RTSP reply
				self.replyRtsp(self.OK_200, seq[1])

		# Process PLAY request
		elif requestType == self.PLAY:
			if self.state == self.READY:
				print("processing PLAY\n")
				self.state = self.PLAYING

				# [FIX-3] Create the RTP socket only once per session. Previously, every PLAY
				# (including after each PAUSE) created a new socket without closing the
				# previous one, leaking one descriptor per pause/play cycle.
				if 'rtpSocket' not in self.clientInfo:
					self.clientInfo["rtpSocket"] = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)

				self.replyRtsp(self.OK_200, seq[1])

				# Create a new thread and start sending RTP packets.
				# The Event is passed as an argument: the thread only sees ITS OWN event, and
				# a quick PAUSE/PLAY cannot "resurrect" an old thread.
				stopEvent = threading.Event()
				self.clientInfo['event'] = stopEvent
				self.clientInfo['worker'] = threading.Thread(
					target=self.sendRtp, args=(stopEvent,), daemon=True,
					name=f"rtp-sender-{self.tag}")
				self.clientInfo['worker'].start()

		# Process PAUSE request
		elif requestType == self.PAUSE:
			if self.state == self.PLAYING:
				print("processing PAUSE\n")
				self.state = self.READY

				# [FIX-3] Wait for the sender thread to actually stop BEFORE confirming.
				# This way no packet leaves after the PAUSE "200 OK", and an immediate PLAY
				# does not run in parallel with the old thread (two senders on the same video).
				self._stopSender()

				self.replyRtsp(self.OK_200, seq[1])

		# Process TEARDOWN request
		elif requestType == self.TEARDOWN:
			print("processing TEARDOWN\n")

			# [FIX-1] Previously, TEARDOWN only closed the UDP socket and left the recv loop
			# alive (and the camera open). Here we stop sending, acknowledge and signal the
			# end of the session; the `finally` in recvRtspRequest releases the rest.
			# It also no longer raises KeyError when a TEARDOWN arrives without a previous
			# PLAY, because _stopSender() uses .get() and does not assume the Event exists.
			self._stopSender()
			self.replyRtsp(self.OK_200, seq[1])
			self.teardownDone = True

	def _stopSender(self):
		"""Stop the RTP sender thread (if any) and wait for it to finish."""
		stopEvent = self.clientInfo.get('event')
		worker = self.clientInfo.get('worker')

		if stopEvent is not None:
			stopEvent.set()

		# The current_thread guard avoids join()ing the thread on itself (deadlock).
		if worker is not None and worker.is_alive() and worker is not threading.current_thread():
			worker.join(timeout=SENDER_JOIN_TIMEOUT)
			if worker.is_alive():
				print(f"[ServerWorker {self.tag}] Warning: the sender thread did not stop in time")

	def _releaseResources(self):
		"""Release everything the session allocated. Idempotent.

		[FIX-3] Order matters: first we stop the sender thread and only then close the UDP
		socket and the camera, so nobody uses a resource that was already released.
		"""
		self._stopSender()

		rtpSocket = self.clientInfo.pop('rtpSocket', None)
		if rtpSocket is not None:
			rtpSocket.close()

		videoStream = self.clientInfo.pop('videoStream', None)
		if videoStream is not None:
			videoStream.release()

		try:
			self.clientInfo['rtspSocket'][0].close()
		except OSError:
			pass

		self.state = self.INIT
		print(f"[ServerWorker {self.tag}] Session closed and resources released")

		# [FIX-4] Last: only notify the Server after everything is released, so the slot
		# (and the webcam, if this was the last viewer) does not count as free too early.
		# Cleared before calling, so the callback runs at most once.
		onClose, self.onClose = self.onClose, None
		if onClose is not None:
			onClose(self)

	def sendRtp(self, stopEvent):
		"""Send RTP packets over UDP, one per frame, until `stopEvent` is set.

		[FIX-2] Pacing: the original waited a FIXED 0.05 s between frames, on top of the time
		to read/resize/encode, so a 30 fps video played in slow motion. Now each frame has
		an ABSOLUTE deadline (start + n * interval), based on a monotonic clock. Since the
		deadline does not depend on how long processing took, the error does not accumulate
		and the video plays at its native speed.

		For the webcam there is no waiting: read() already blocks until the camera delivers
		the next frame, so the camera itself sets the pace.
		"""
		videoStream = self.clientInfo['videoStream']
		rtpSocket = self.clientInfo['rtpSocket']
		address = self.clientInfo['rtspSocket'][1][0]
		port = int(self.clientInfo['rtpPort'])

		interval = videoStream.frameInterval
		nextDeadline = time.monotonic()
		readFailures = 0
		sendFailures = 0

		while not stopEvent.is_set():
			if not videoStream.isLive:
				now = time.monotonic()
				if nextDeadline > now:
					# Event.wait() plays the role of sleep(), but wakes up immediately if
					# PAUSE/TEARDOWN signals it; stopping stays responsive.
					if stopEvent.wait(nextDeadline - now):
						break
				elif now - nextDeadline > interval:
					# Late by more than one frame (slow disk, busy CPU): instead of firing
					# several frames at once to "catch up" (which would overflow the
					# client's buffer), re-anchor the clock and carry on.
					nextDeadline = now
				nextDeadline += interval

			data = videoStream.nextFrame()

			if data is None:
				# [FIX-1] End of the video (or camera failure). Previously the thread kept
				# waking up every 50 ms forever without sending anything.
				readFailures += 1
				limit = LIVE_MAX_READ_FAILURES if videoStream.isLive else 1
				if readFailures >= limit:
					print(f"[ServerWorker {self.tag}] End of stream: no more frames to send")
					break
				if stopEvent.wait(READ_RETRY_DELAY):
					break
				continue
			readFailures = 0

			try:
				packet = self.makeRtp(data, videoStream.frameNbr())
				rtpSocket.sendto(packet, (address, port))
				sendFailures = 0
			except Exception as e:
				# Log only the first failure of the sequence (a traceback per frame would
				# flood the console at 30 lines/s) and give up if it persists.
				sendFailures += 1
				if sendFailures == 1:
					print(f"[ServerWorker {self.tag}] Error sending RTP: {e!r}")
				if sendFailures >= MAX_SEND_FAILURES:
					print(f"[ServerWorker {self.tag}] Too many consecutive send failures; stopping")
					break

	def makeRtp(self, payload, frameNbr):
		"""RTP-packetize the video data."""
		version = 2
		padding = 0
		extension = 0
		cc = 0
		marker = 0
		pt = 26  # MJPEG type
		# [FIX-7] The RTP sequence field is 16 bits and wraps to zero after 65535. Without the
		# mask, RtpPacket.encode overflowed from frame 65536 on (~36 min at 30 fps) and the
		# server stopped sending. The client (RtpStats) understands the wrap-around.
		seqnum = frameNbr & 0xFFFF
		ssrc = 0

		rtpPacket = RtpPacket()

		rtpPacket.encode(version, padding, extension, cc, seqnum, marker, pt, ssrc, payload)

		return rtpPacket.getPacket()

	def replyRtsp(self, code, seq):
		"""Send RTSP reply to the client."""
		# [FIX-5] Previously only 200 was sent; 404 and 500 just printed to the console and
		# the client never found out. All replies keep the same 3-line format (status,
		# CSeq, Session) that the client's parseRtspReply expects. On errors, Session is 0
		# (there is no session yet).
		if code == self.OK_200:
			status = 'RTSP/1.0 200 OK'
			session = self.clientInfo.get('session', 0)
		elif code == self.FILE_NOT_FOUND_404:
			print("404 NOT FOUND")
			status = 'RTSP/1.0 404 NOT FOUND'
			session = 0
		elif code == self.CON_ERR_500:
			print("500 CONNECTION ERROR")
			status = 'RTSP/1.0 500 CONNECTION ERROR'
			session = 0
		else:
			return

		reply = status + '\nCSeq: ' + seq + '\nSession: ' + str(session)
		connSocket = self.clientInfo['rtspSocket'][0]
		connSocket.send(reply.encode())
