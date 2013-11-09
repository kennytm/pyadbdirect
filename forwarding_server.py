#!/usr/bin/env python3

import struct

HEADER_STRUCT = struct.Struct('<4sIIII4s')

"""
usage 'pinhole port host [newport]'

Pinhole forwards the port to the host specified.
The optional newport parameter may be used to
redirect to a different port.

eg. pinhole 80 webserver
    Forward all incoming WWW sessions to webserver.

    pinhole 23 localhost 2323
    Forward all telnet sessions to port 2323 on localhost.
"""

import sys
from socket import *
from threading import Thread
import time

LOGGING = 1


class PipeThread( Thread ):
    pipes = []
    def __init__( self, source, sink, direction, port ):
        Thread.__init__( self )
        self.source = source
        self.sink = sink

        self.direction = direction
        self.port = port

        PipeThread.pipes.append( self )

    def run( self ):
        while 1:
            try:
                data = self.source.recv( 1024 )
                if not data: break

                print(self.direction, self.port, HEADER_STRUCT.unpack_from(data), data[24:])

                self.sink.send( data )
            except:
                break

        PipeThread.pipes.remove( self )

class Pinhole( Thread ):
    def __init__( self, port, newhost, newport ):
        Thread.__init__( self )
        self.newhost = newhost
        self.newport = newport
        self.sock = socket( AF_INET, SOCK_STREAM )
        self.sock.bind(( '', port ))
        self.sock.listen(5)

    def run( self ):
        while 1:
            newsock, address = self.sock.accept()
            fwd = socket( AF_INET, SOCK_STREAM )
            fwd.connect(( self.newhost, self.newport ))
            PipeThread( newsock, fwd, 'send', self.newport ).start()
            PipeThread( fwd, newsock, 'recv', self.newport ).start()

if __name__ == '__main__':


    Pinhole( 5559, '192.168.56.101', 5555 ).start()
    #Pinhole( 5558, '127.0.0.1', 5554 ).start()
