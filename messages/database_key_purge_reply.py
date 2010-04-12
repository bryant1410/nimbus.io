# -*- coding: utf-8 -*-
"""
database_key_purge_reply.py

DatabaseKeyPurgeReply message
"""
import struct

from diyapi_tools.marshalling import marshall_string, unmarshall_string

# 32s - request-id 32 char hex uuid
# B   - result: 0 = success 
# Q   - total size
_header_format = "!32sBQ"
_header_size = struct.calcsize(_header_format)

class DatabaseKeyPurgeReply(object):
    """AMQP message to insert a key in the database"""

    routing_tag = "database_key_purge_reply"
   
    successful = 0
    error_database_failure = 2
    error_too_old = 2

    def __init__(
        self, request_id, result, total_size=0, error_message=""
    ):
        self.request_id = request_id
        self.result = result
        self.total_size = total_size
        self.error_message = error_message

    @property
    def error(self):
        return self.result != DatabaseKeyPurgeReply.successful

    @classmethod
    def unmarshall(cls, data):
        """return a DatabaseKeyPurge message"""
        pos = 0
        request_id, result, total_size = struct.unpack(
            _header_format, data[pos:pos+_header_size]
        )
        pos += _header_size

        if result == 0:
            return DatabaseKeyPurgeReply(request_id, result, total_size)

        (error_message, pos) = unmarshall_string(data, pos)

        return DatabaseKeyPurgeReply(
            request_id, result, total_size, error_message
        )

    def marshall(self):
        """return a data string suitable for transmission"""
        header = struct.pack(
            _header_format, 
            self.request_id, 
            self.result, 
            self.total_size 
        )
        packed_error_message = marshall_string(self.error_message)

        return "".join([header, packed_error_message, ])

