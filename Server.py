import socket
import threading

from ServerWorker import ServerWorker

# Default RTSP port (the same convention used by tools such as VLC and ffplay). Fixed on
# purpose: with a well-known port the firewall needs a single rule and the client does not
# depend on the API picking anything at random.
DEFAULT_PORT = 8554

# Cap on simultaneous sessions. Each session costs threads and file descriptors; without a
# limit, a single client (or a buggy loop) could exhaust the server's resources.
MAX_SESSIONS = 32

# How often the accept() loop wakes up to check whether the server was stopped. A blocking
# accept() cannot be interrupted in a portable way from another thread.
ACCEPT_POLL_INTERVAL = 1.0


class Server:
	"""SINGLE RTSP server: one fixed port serving several clients at the same time.

	Each accepted connection becomes an independent session, handled by a ServerWorker with
	its own threads and resources. The server lives as long as the application does (see
	`lifespan` in api_server.py), and cleaning up each session is the ServerWorker's job.

	[FIX-4] Previously the API created a new Server per session, each with a random port
	and a single client. That scattered ports across the machine, prevented central control
	of the sessions and made the second "live" session fight the first one for the webcam.
	"""

	def __init__(self, server_port=DEFAULT_PORT, max_sessions=MAX_SESSIONS):
		self.server_port = server_port
		self.max_sessions = max_sessions

		self._stopRequested = threading.Event()
		self._running = False

		# Protects _workers, which is accessed by the accept thread (adds/limits) and by
		# each session's thread (they remove themselves when they finish).
		self._lock = threading.Lock()
		self._workers = set()

		# bind/listen happen here, synchronously, and not inside run(). This way, if the
		# port is busy, the error propagates to whoever created the Server (the API fails
		# to start, instead of the thread dying silently), and the port already accepts
		# connections when the constructor returns.
		self.rtspSocket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
		try:
			self.rtspSocket.bind(('', self.server_port))
			self.rtspSocket.listen(5)
		except OSError:
			self.rtspSocket.close()
			raise
		self.rtspSocket.settimeout(ACCEPT_POLL_INTERVAL)

	@property
	def activeSessions(self):
		"""Number of sessions in progress."""
		with self._lock:
			return len(self._workers)

	def run(self):
		"""Accept connections until stop() is called. Blocks, so run it in a thread."""
		print(f"\n[RTSP Server] Started and listening on port {self.server_port}")
		self._running = True
		try:
			while not self._stopRequested.is_set():
				try:
					connSocket, clientAddr = self.rtspSocket.accept()
				except socket.timeout:
					continue
				except OSError as e:
					# An isolated error (e.g. a client that reset before accept(), common
					# on Windows) must NOT take down a server that serves several clients.
					# We only leave if the server was stopped or the listening socket closed.
					if self._stopRequested.is_set() or self.rtspSocket.fileno() == -1:
						break
					print(f"[RTSP Server] Error in accept(): {e!r}")
					# Avoids a tight loop if the error persists (e.g. out of descriptors).
					self._stopRequested.wait(0.1)
					continue

				self._startSession(connSocket, clientAddr)
		finally:
			self.rtspSocket.close()
			self._running = False
			print("[RTSP Server] Stopped")

	def _startSession(self, connSocket, clientAddr):
		"""Create the ServerWorker for a newly accepted connection, honoring the session limit."""
		# The accepted socket must not inherit the listener's timeout: the ServerWorker
		# waits for requests in a blocking way, for an indefinite time.
		connSocket.settimeout(None)

		with self._lock:
			full = len(self._workers) >= self.max_sessions
			if not full:
				worker = ServerWorker(
					{'rtspSocket': (connSocket, clientAddr)}, onClose=self._forget)
				# Registered BEFORE starting, so _forget() always finds the worker even
				# if the session ends immediately.
				self._workers.add(worker)

		if full:
			print(
				f"[RTSP Server] Limit of {self.max_sessions} sessions reached; "
				f"refusing {clientAddr[0]}:{clientAddr[1]}")
			connSocket.close()
			return

		print(
			f"[RTSP Server] Client {clientAddr[0]}:{clientAddr[1]} connected "
			f"({self.activeSessions} active session(s))")
		worker.run()

	def _forget(self, worker):
		"""Called by the ServerWorker when its session ends, to free the slot."""
		with self._lock:
			self._workers.discard(worker)

	def stop(self):
		"""Stop accepting clients and end the sessions in progress.

		Returns right away; the run() loop exits within ACCEPT_POLL_INTERVAL and each
		session cleans itself up (camera, sockets) in its own thread.
		"""
		self._stopRequested.set()

		# If run() never got to run, nobody else will close the listening socket.
		if not self._running:
			try:
				self.rtspSocket.close()
			except OSError:
				pass

		with self._lock:
			workers = list(self._workers)
		for worker in workers:
			worker.shutdown()
