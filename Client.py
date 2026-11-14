from tkinter import *
import tkinter.messagebox
from PIL import Image, ImageTk
import socket, threading, sys, traceback, os
import time
import sys
from RtpPacket import RtpPacket
from RtpStats import RtpStats, formatBitrate
import io

CACHE_FILE_NAME = "cache-"
CACHE_FILE_EXT = ".jpg"

# [FIX-6] 65535 is the largest possible UDP datagram. The old recv(20480) cut every frame
# bigger than 20 KB: silently on Linux, and as an error on Windows (where the dropped
# packet then showed up as fake packet loss).
MAX_RTP_DATAGRAM = 65535

# How often (in seconds) the current frame rate / data rate / loss line is printed.
STATS_REPORT_INTERVAL = 1.0

class Client:
	INIT = 0
	READY = 1
	PLAYING = 2
	state = INIT

	SETUP = 0
	PLAY = 1
	PAUSE = 2
	TEARDOWN = 3

	# Initiation..
	def __init__(self, master, serveraddr, serverport, rtpport, filename):
		self.master = master
		self.master.protocol("WM_DELETE_WINDOW", self.handler)
		self.createWidgets()
		self.serverAddr = serveraddr
		self.serverPort = int(serverport)
		self.rtpPort = int(rtpport)
		self.fileName = filename
		self.rtspSeq = 0
		self.sessionId = 0
		self.requestSent = -1
		self.teardownAcked = 0
		self.connectToServer()
		self.frameNbr = 0
		self.playEvent = threading.Event()
		self.playEvent.clear()
		# [FIX-6] All reception statistics (loss, frame rate, data rate) live in RtpStats.
		self.stats = RtpStats()

	def createWidgets(self):
		"""Build GUI."""
		# Create Setup button
		#self.setup = Button(self.master, width=20, padx=3, pady=3)
		#self.setup["text"] = "Setup"
		#self.setup["command"] = self.setupMovie
		#self.setup.grid(row=1, column=0, padx=2, pady=2)

		# Create Play button
		self.start = Button(self.master, width=20, padx=3, pady=3)
		self.start["text"] = "Play"
		self.start["command"] = self.playMovie
		self.start.grid(row=1, column=1, padx=2, pady=2)

		# Create Pause button
		self.pause = Button(self.master, width=20, padx=3, pady=3)
		self.pause["text"] = "Pause"
		self.pause["command"] = self.pauseMovie
		self.pause.grid(row=1, column=2, padx=2, pady=2)

		# Create Teardown button
		self.teardown = Button(self.master, width=20, padx=3, pady=3)
		self.teardown["text"] = "Teardown"
		self.teardown["command"] =  self.exitClient
		self.teardown.grid(row=1, column=3, padx=2, pady=2)

		# Create a label to display the movie
		self.label = Label(self.master, height=19)
		self.label.grid(row=0, column=0, columnspan=4, sticky=W+E+N+S, padx=5, pady=5)

	#def setupMovie(self):
	#	"""Setup button handler."""
	#	if self.state == self.INIT:
	#		self.sendRtspRequest(self.SETUP)

	def exitClient(self):
		"""Teardown button handler."""
		# Showing Statistics
		# [FIX-6] The old numbers were wrong (see RtpStats), and there was no frame rate.
		stats = self.stats.snapshot()
		if stats['received'] == 0:
			msg = "No video data was received."
		else:
			msg = "\n".join([
				f"Frames received: {stats['received']} of {stats['expected']} expected",
				f"Packet loss: {stats['lossPercent']:.2f}% ({stats['lost']} lost)",
				f"Average frame rate: {stats['avgFps']:.1f} fps",
				f"Average data rate: {formatBitrate(stats['avgBitrate'])}",
				f"Playback time: {stats['playTime']:.1f} s",
				f"Late / out-of-order packets: {stats['late']}",
			])
		tkinter.messagebox.showinfo('Video Statistics', msg)
		
		self.sendRtspRequest(self.TEARDOWN)
		self.master.destroy() # Close the gui window
		# Delete the cache image from video. If the user closes before any frame arrives,
		# the file was never created; that is not an error (it used to break the shutdown).
		try:
			os.remove(CACHE_FILE_NAME + str(self.sessionId) + CACHE_FILE_EXT)
		except FileNotFoundError:
			pass

	def pauseMovie(self):
		"""Pause button handler."""
		if self.state == self.PLAYING:
			self.sendRtspRequest(self.PAUSE)


	def playMovie(self):
		"""Play button handler."""
		print(self.state)
		if self.state == self.INIT:
			self.sendRtspRequest(self.SETUP)
			if self.state == self.INIT:
				time.sleep(1) #print('Waiting for the SETUP ACK...')
		if self.state == self.READY:
			# Create a new thread to listen for RTP packets
			if self.rtspSeq == 1:
				threading.Thread(target=self.listenRtp).start()
			#self.playEvent = threading.Event()
			#self.playEvent.clear()
			self.sendRtspRequest(self.PLAY)

	def listenRtp(self):
		"""Listen for RTP packets."""
		lastReport = time.monotonic()
		while True:
			try:
				data = self.rtpSocket.recv(MAX_RTP_DATAGRAM)
			except socket.timeout:
				# Nothing arrived within the socket timeout (0.5 s). Fall through to the
				# TEARDOWN check below; this is how the loop notices it has to stop.
				data = None
			except OSError:
				# The socket was closed or broke: there is nothing left to listen to.
				break

			if data:
				try:
					rtpPacket = RtpPacket()
					rtpPacket.decode(data)

					# [FIX-6] update() counts the packet for the statistics and returns its
					# EXTENDED sequence number, which keeps growing after the 16-bit wrap
					# (65535 -> 65536 instead of 0). Comparing it with the last frame shown
					# is what keeps the "discard late packet" rule working after the wrap.
					currFrameNbr = self.stats.update(rtpPacket.seqNum(), len(data))

					if currFrameNbr > self.frameNbr: # Discard the late packet
						self.frameNbr = currFrameNbr
						self.updateMovie(self.writeFrame(rtpPacket.getPayload()))
				except Exception as e:
					# [FIX-6] The old bare `except:` swallowed everything, including bugs in
					# the statistics code. Now a malformed packet or an undecodable JPEG is
					# reported and skipped, and the listener keeps running.
					print('Error handling RTP packet: %r' % (e,))

				# One status line per second instead of three prints per packet.
				now = time.monotonic()
				if now - lastReport >= STATS_REPORT_INTERVAL:
					lastReport = now
					print(self.stats.summaryLine())

			# Upon receiving ACK for TEARDOWN request,
			# close the RTP socket
			if self.teardownAcked == 1:
				try:
					self.rtpSocket.shutdown(socket.SHUT_RDWR)
				except OSError:
					# shutdown() always fails on an unconnected UDP socket. Before, that error
					# escaped and killed the thread before close() was reached.
					pass
				self.rtpSocket.close()
				break

	def writeFrame(self, data):
		"""Write the received frame to a temp image file. Return the image file."""
		cachename = CACHE_FILE_NAME + str(self.sessionId) + CACHE_FILE_EXT
		file = open(cachename, "wb")
		file.write(data)
		file.close()

		return cachename

	def updateMovie(self, imageFile):
		"""Update the image file as video frame in the GUI."""
		photo = ImageTk.PhotoImage(Image.open(imageFile))
		self.label.configure(image = photo, height=288)
		self.label.image = photo

	def connectToServer(self):
		"""Connect to the Server. Start a new RTSP/TCP session."""
		self.rtspSocket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
		try:
			self.rtspSocket.connect((self.serverAddr, self.serverPort))
			threading.Thread(target=self.recvRtspReply).start()
		except:
			tkinter.messagebox.showwarning('Connection Failed', 'Connection to \'%s\' failed.' %self.serverAddr)


	def sendRtspRequest(self, requestCode):
		"""Send RTSP request to the server."""

		# SETUP implementation
		if requestCode == self.SETUP and self.state == self.INIT:
			#Reseting the teardown
			self.teardownAcked = 0
			# Updating the sequence number, so we know which request we are doing.
			self.rtspSeq += 1

			# Writing the exact RTSP request string for sending the SETUP request (1st line) 
			request = 'SETUP ' + str(self.fileName) + ' RTSP/1.0\n'

			# Writing the sequence for the request (2nd line)
			request += 'CSeq: '+ str(self.rtspSeq) + '\n' 

			# Writing the protocol for transference, type and port of control
			request += 'Transport: RTP/AVP; unicast; '+str(self.rtpPort)


			# Updating the RTSP request type
			self.requestSent = self.SETUP

		# PLAY implementation
		elif requestCode == self.PLAY and self.state == self.READY:
			# Updating the sequence number, so we know which request we are doing.
			self.rtspSeq += 1

			# Writing the exact RTSP request string for sending the PLAY request (1st line)
			request = 'PLAY ' + str(self.fileName) + ' RTSP/1.0\n'

			# Writing the sequence for the request (2nd line)
			request += 'CSeq: '+ str(self.rtspSeq) + '\n' 

			# Writing the session number
			request += 'Session: '+ str(self.sessionId)
	
			# Updating the RTSP request type
			self.requestSent = self.PLAY

		# PAUSE implementation
		elif requestCode == self.PAUSE and self.state == self.PLAYING:
			# Updating the sequence number, so we know which request we are doing.
			self.rtspSeq += 1

			# Writing the exact RTSP request string for sending the PAUSE request (1st line)
			request = 'PAUSE ' + str(self.fileName) + ' RTSP/1.0\n'

			# Writing the sequence for the request (2nd line)
			request += 'CSeq: ' + str(self.rtspSeq) + '\n'

			# Writing the session number
			request += 'Session: '+ str(self.sessionId)

			# Updating the RTSP request type
			self.requestSent = self.PAUSE

		elif requestCode == self.TEARDOWN:
			# Updating the sequence number, so we know which request we are doing.
			self.rtspSeq += 1

			# Writing the exact RTSP request string for sending the TEARDOWN request (1st line)
			request = 'TEARDOWN ' + str(self.fileName) + ' RTSP/1.0\n'

			# Writing the sequence for the request (2nd line)
			request += 'CSeq: ' + str(self.rtspSeq) + '\n'

			# Writing the session number
			request += 'Session ' + str(self.sessionId)

			# Updating the RTSP request type
			self.requestSent = self.TEARDOWN

		# In case that don't match any if, just go back
		else:
			return

		# Sending the message through the socket 
		self.rtspSocket.send(request.encode('utf-8'))
		print('\nDATA Sent: \n' + request)


	def recvRtspReply(self):
		"""Receive RTSP reply from the server."""
		while True:
			try:
				reply = self.rtspSocket.recv(1024).decode('utf-8')
			except OSError:
				# Connection reset/aborted by the server: treat it as a disconnect.
				break

			if not reply:
				# [FIX-1] recv() returns b'' once the server has closed the connection. The
				# server now closes it when the session ends, and without this break the
				# loop would spin at 100% CPU, since recv() returns immediately on a closed socket.
				break

			self.parseRtspReply(reply)

			# Close the RTSP socket upon requesting Teardown
			if self.requestSent == self.TEARDOWN:
				try:
					self.rtspSocket.shutdown(socket.SHUT_RDWR)
				except OSError:
					# The server may already have closed its end after acknowledging TEARDOWN.
					pass
				self.rtspSocket.close()
				break

	def parseRtspReply(self, data):
		"""Parse the RTSP reply from the server."""
		lines = data.split('\n')
		seqNum = int(lines[1].split(' ')[1])
	
		# Process only if the server reply's sequence number is the same as the request's
		if seqNum == self.rtspSeq:
			session = int(lines[2].split(' ')[1])
			# New RTSP session ID
			if self.sessionId == 0:
				self.sessionId = session
				print('Session ID = ', session)

			# Process only if the session ID is the same
			if self.sessionId == session:
				if int(lines[0].split(' ')[1]) == 200:
					if self.requestSent == self.SETUP:
						# Starting the RTP port. This comes BEFORE changing the state: as soon as
						# the state becomes READY, the main thread (playMovie) may start
						# listenRtp, which uses the rtpSocket. Over a network, with the reply
						# arriving on another thread, this race window is real.
						self.openRtpPort()

						# Update the state of the client 
						self.state = self.READY

						#Looking for the session ID in our string received.
						for line in lines:
							if 'Session' in line:
								try:
									self.sessionId = int(line.split(" ")[1])
								except:
									tkinter.messagebox.showwarning('Session ID FAILURE', 'Unable to read the session ID')

					elif self.requestSent == self.PLAY:
						# Update the state of the client
						self.state = self.PLAYING
						# Start processing the UDP Segments arriving, showing the video
						self.playEvent.set()

					elif self.requestSent == self.PAUSE:
						# Update the state of the client
						self.state = self.READY
						# Pause the processing
						self.playEvent.clear()

					elif self.requestSent == self.TEARDOWN:
						# Update the state of the client
						self.state == self.INIT
						self.teardownAcked = 1
						

					

	def openRtpPort(self):
		"""Open RTP socket binded to a specified port."""

		# Creating the RTP Socket using IPv4 and UDP protocol
		self.rtpSocket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)

		#Setting timeout value to avoid lock if the internet fails
		self.rtpSocket.settimeout(0.5)

		try:
			#Binding the socket to the specified port in the execution, and receiveng from all the network ports from the computer.
			self.rtpSocket.bind(('', self.rtpPort))

		except:
			tkinter.messagebox.showwarning('Binding RTP failed', 'Unable to bind the RTP Socket')



	def handler(self):
		"""Handler on explicitly closing the GUI window."""
		self.pauseMovie()
		if tkinter.messagebox.askokcancel("Quit?", "Are you sure you want to quit?"):
			self.exitClient()
		else: # When the user presses cancel, resume playing.
			self.playMovie()
