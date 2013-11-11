#!/usr/bin/env python3
#
#-------------------------------------------------------------------------------
#
# Copyright 2013 HiHex Ltd.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#
#-------------------------------------------------------------------------------

import asyncore
import collections
import errno
import itertools
import queue
import select
import shlex
import socket
import struct
import threading
import time

HEADER_STRUCT = struct.Struct('<4sIIII4s')

CMD_CONNECT = b'CNXN'
CMD_OPEN = b'OPEN'
CMD_OKAY = b'OKAY'
CMD_WRITE = b'WRTE'
CMD_CLOSE = b'CLSE'

ADBP_VERSION = 0x01000000
ADBP_MAX_DATA = 4096


class ADBError(Exception):
    '''An ADB protocol error.'''
    pass


class _Service(object):
    __slots__ = ('service', 'remote_id', 'first_message')

    def __init__(self, service):
        self.service = service
        self.remote_id = 0
        self.first_message = None


class _ADBSocket(object):
    '''A wrapper of socket which handles ADB messages.

    This class registers a TCP socket which directly communicates with the
    Android device, using the ADB protocol. The socket will be run on a separate
    thread. The decoded commands received will be placed into a queue.

    :doc:`Description of the ADB protocol <https://android.googlesource.com/platform/system/core/+/master/adb/protocol.txt>`
    '''

    def __init__(self, addr, recv_msg_queue):
        '''Construct the socket.

        :param addr: The host and port of the target device, e.g.
                     ``('192.168.56.101', 5555)``.
        :param recv_msg_queue: A :cls:`Queue` which decoded objects will be
                               placed into.
        '''
        self._socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._socket.setblocking(False)
        self._socket_thread = threading.Thread(target=self._run)
        self._addr = addr
        self._send_buffer = bytearray()
        self._recv_buffer = None
        self._pending_recv_msg = None
        self._recv_msg_queue = recv_msg_queue
        self._is_closed_event = threading.Event()
        self._send_lock = threading.Lock()

        self.send_msg(CMD_CONNECT, ADBP_VERSION, ADBP_MAX_DATA, b'host::\0')


    def send_msg(self, cmd, arg0, arg1, data):
        '''Send an ADB message.'''
        header = HEADER_STRUCT.pack(cmd, arg0, arg1,
                                    len(data), sum(data) & 0xffffffff,
                                    bytes(c ^ 0xff for c in cmd))
        with self._send_lock:
            self._send_buffer.extend(header)
            self._send_buffer.extend(data)


    def start(self):
        '''Start the socket's send/receive thread.'''
        self._socket_thread.start()


    def close(self):
        '''Terminate the socket's send/receive thread.'''
        self._is_closed_event.set()


    def _run(self):
        try:
            err = self._socket.connect_ex(self._addr)
            if err not in (0, errno.EINPROGRESS, errno.EALREADY, errno.EWOULDBLOCK, errno.EISCONN):
                raise socket.error(err)

            while not self._is_closed_event.is_set():
                r = [self._socket]
                w = [self._socket] if self._send_buffer else []

                (r, w, _) = select.select(r, w, [], 1.0)

                if r:
                    received = self._socket.recv(4096)
                    if not received:
                        break # socket already closed.

                    while received:
                        if not self._pending_recv_msg:
                            self._pending_recv_msg = HEADER_STRUCT.unpack_from(received)
                            self._recv_buffer = received[HEADER_STRUCT.size:]
                        else:
                            if not isinstance(self._recv_buffer, bytearray):
                                self._recv_buffer = bytearray(self._recv_buffer)
                            self._recv_buffer.extend(received)

                        (cmd, arg0, arg1, length, checksum, magic) = self._pending_recv_msg
                        if any(a ^ b != 0xff for a, b in zip(cmd, magic)):
                            raise ADBError('Invalid magic received.')

                        if len(self._recv_buffer) < length:
                            break
                        else:
                            data = self._recv_buffer[:length]
                            received = self._recv_buffer[length:]
                            self._pending_recv_msg = None

                            if sum(data) != checksum:
                                raise ADBError('Invalid checksum.')
                            self._recv_msg_queue.put((cmd, arg0, arg1, data))

                if w:
                    with self._send_lock:
                        send_length = self._socket.send(self._send_buffer)
                    del self._send_buffer[:send_length]

        finally:
            self._socket.close()


class ADBConnection(object):
    '''Represents an ADB connection to an Android device.'''

    def __init__(self, host, port=5555):
        '''Construct a connection to an Android device at *host*.'''

        self._msg_queue = queue.Queue()
        self._msg_thread = threading.Thread(target=self._run)
        self._host = host
        self._port = port
        self._socket = _ADBSocket((host, port), self._msg_queue)
        self._services = {}
        self._service_counter = itertools.count(1)
        self._service_closed_condition = threading.Condition(threading.Lock())


    def register(self, service_name, service):
        '''Register a service.

        A *service* is a generator. It will be fed output from the device, and
        the generator can take action by yielding the appropriate result.

        :param service_name: The name of the service, e.g. ``shell:ls``. The
                             interpretation depends on the Android device's ADB
                             daemon.
        :param service: A generator which will receive the data and generate
                        replies.
        :rtype: A unique ID representing this service in this connection.
        '''
        service_gen = iter(service)
        service_id = next(self._service_counter)
        service = _Service(service_gen)
        with self._service_closed_condition:
            self._services[service_id] = service
        encoded_service_name = service_name.encode() + b'\0'
        self._socket.send_msg(CMD_OPEN, service_id, 0, encoded_service_name)
        service.first_message = next(service_gen)

        return service_id


    def close_service(self, service_id):
        '''Close a service now.'''

        with self._service_closed_condition:
            service = self._services.pop(service_id, None)
            if not service:
                return
            if not self._services:
                self._service_closed_condition.notify()
        service.service.close()
        self._socket.send_msg(CMD_CLOSE, service_id, service.remote_id, b'')

    def _run(self):
        while True:
            msg = self._msg_queue.get()
            if not msg:
                break

            (cmd, remote_id, service_id, data) = msg
            if cmd == CMD_CONNECT:
                if remote_id != ADBP_VERSION:
                    raise ADBError('Unknown protocol')
                continue

            try:
                service = self._services[service_id]
            except KeyError:
                continue

            if cmd == CMD_OKAY:
                service.remote_id = remote_id
                if service.first_message is not None:
                    self._socket.send_msg(CMD_WRITE, service_id, remote_id,
                                           service.first_message)
                    service.first_message = None
            elif cmd == CMD_WRITE:
                self._socket.send_msg(CMD_OKAY, service_id, remote_id, b'')
                try:
                    reply = service.service.send(data)
                    if reply is not None:
                        self._socket.send_msg(CMD_WRITE, service_id, remote_id,
                                               reply)
                except StopIteration:
                    self.close_service(service_id)
            elif cmd == CMD_CLOSE:
                self.close_service(service_id)
            else:
                raise ADBError('Unknown command')


    def start(self):
        '''Start all threads running the connection.

        This method is called when the connection is used as a context manager.
        Typically one uses a connection as::

            with ADBConnection(host) as conn:
                # do something with conn
        '''
        self._socket.start()
        self._msg_thread.start()

    def close(self):
        '''Close the connection.'''
        self._socket.close()
        self._msg_queue.put(None)

    def poke(self, service_id, message=None):
        '''Wake up the service and fake a message.

        The service will be fed *message* as if it is sent from the Android
        device. This method is usually used to ensure a response from
        user-initiated action.
        '''
        try:
            service = self._services[service_id]
        except KeyError:
            raise ADBError('Unknown service')
        self._msg_queue.put((CMD_WRITE, service.remote_id, service_id, None))

    def __enter__(self):
        self.start()
        return self

    def __exit__(self, *args):
        self.close()

    def wait(self):
        '''Wait until all services have been closed.'''

        with self._service_closed_condition:
            while self._services:
                self._service_closed_condition.wait()

#{{{ Services ------------------------------------------------------------------

def simple_service(callback):
    '''A service which simply reads and reply all data using a function.

    :param callback: A function object taking a "bytes" object, being the data
                     just being read from the device, another "bytes" object as
                     reply. Both "bytes" object may be None, indicating the lack
                     of data.
    '''

    received = None
    while True:
        received = yield callback(received)


def simple_text_service(callback, encoding='utf-8'):
    '''A service which simply reads and reply all text, using a function.

    Example of printing the content of logcat::

        with ADBConnection('192.168.56.101') as conn:
            conn.register('shell:export ANDROID_LOG_TAGS="";exec logcat',
                          simple_text_service(print))
            conn.wait()

    :param callback: A function object taking a "str" object, being the data
                     just being read from the device, another "str" object as
                     reply. Both "str" object may be None, indicating the lack
                     of data.
    :param encoding: The text encoding of the data being sent/received.
    '''

    received = None
    while True:
        if received is not None:
            received = received.decode(encoding)
        reply = callback(received)
        if reply is not None:
            reply = reply.encode(encoding)
        received = yield reply

#}}}

#{{{ Service names -------------------------------------------------------------

def logcat_name(filters=None, format=None, clear=False, binary=False):
    '''The name of a LogCat service.

    The *filters* should be an iterable of pairs of the form
    ``[('tag1', 'v'), ('tag2', 'e'), ...]``. The first element is the log
    component tag, and the second element is the priority. The tag can be
    ``'*'``.

    +----------+-----------------------------+
    | Priority | Definition                  |
    +==========+=============================+
    | 'v'      | Verbose                     |
    +----------+-----------------------------+
    | 'd'      | Debug                       |
    +----------+-----------------------------+
    | 'i'      | Info                        |
    +----------+-----------------------------+
    | 'w'      | Warn                        |
    +----------+-----------------------------+
    | 'e'      | Error                       |
    +----------+-----------------------------+
    | 'f'      | Fatal                       |
    +----------+-----------------------------+
    | 's'      | Silent (supress all output) |
    +----------+-----------------------------+

    The *format* can be one of:

    * brief
    * process
    * tag
    * thread
    * raw
    * time
    * threadtime
    * long

    :param filters: The tag filters.
    :param format: The output format.
    :param clear: If true, clear the logcat history and exit immediately.
    :param binary: If true, output the data in binary format.
    '''

    args = []
    if clear:
        args.append('-c')
    if binary:
        args.append('-B')
    if format:
        args.extend(['-v', format])
    if filters:
        args.append(',')
        for tag, priority in filters:
            args.append(shlex.quote(tag) + ':' + priority)

    return 'shell:export ANDROID_LOG_TAGS="";exec logcat ' + ' '.join(args)


#}}}

if __name__ == '__main__':
    with ADBConnection('192.168.56.101') as conn:
        conn.register('shell:export ANDROID_LOG_TAGS="";exec logcat',
                      simple_text_service(lambda b: print(b, end='')))
        conn.wait()

