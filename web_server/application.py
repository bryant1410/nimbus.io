# -*- coding: utf-8 -*-
"""
application.py

The nimbus.io wsgi application
"""
from base64 import b64encode
import logging
import os
import random
import zlib
import hashlib
import json
from itertools import chain
from binascii import hexlify
import urllib
import time

from webob.dec import wsgify
from webob import exc
from webob import Response

from tools.data_definitions import create_timestamp, nimbus_meta_prefix

from tools.collection import get_username_and_collection_id, \
        get_collection_id, \
        compute_default_collection_name, \
        create_collection, \
        list_collections, \
        delete_collection

from web_server.central_database_util import get_cluster_row
from web_server.exceptions import SpaceAccountingServerDownError, \
        SpaceUsageFailedError, \
        RetrieveFailedError, \
        ArchiveFailedError, \
        DestroyFailedError, \
        CollectionError
from web_server.data_writer_handoff_client import \
        DataWriterHandoffClient
from web_server.data_writer import DataWriter
from web_server.data_slicer import DataSlicer
from web_server.zfec_segmenter import ZfecSegmenter
from web_server.archiver import Archiver
from web_server.destroyer import Destroyer
from web_server.listmatcher import Listmatcher
from web_server.space_usage_getter import SpaceUsageGetter
from web_server.stat_getter import StatGetter
from web_server.retriever import Retriever
from web_server.meta_manager import get_meta, list_meta
from web_server.conjoined_manager import list_conjoined_archives, \
        start_conjoined_archive, \
        abort_conjoined_archive, \
        finish_conjoined_archive
from web_server.url_discriminator import parse_url, \
        action_list_collections, \
        action_create_collection, \
        action_delete_collection, \
        action_space_usage, \
        action_archive_key, \
        action_list_keys, \
        action_retrieve_key, \
        action_delete_key, \
        action_head_key


_node_names = os.environ['NIMBUSIO_NODE_NAME_SEQ'].split()
_reply_timeout = float(
    os.environ.get("NIMBUSIO_REPLY_TIMEOUT",  str(5 * 60.0))
)
_slice_size = 1024 * 1024    # 1MB
_min_connected_clients = 8
_min_segments = 8
_max_segments = 10
_handoff_count = 2

_s3_meta_prefix = "x-amz-meta-"
_sizeof_s3_meta_prefix = len(_s3_meta_prefix)

def _build_meta_dict(req_get):
    """
    create a dict of meta values, conveting the aws prefix to ours
    """
    meta_dict = dict()
    for key in req_get:
        if key.startswith(_s3_meta_prefix):
            converted_key = "".join(
                    [nimbus_meta_prefix, key[_sizeof_s3_meta_prefix:]]
                )
            meta_dict[converted_key] = req_get[key]
        elif key.startswith(nimbus_meta_prefix):
            meta_dict[key] = req_get[key]

    return meta_dict

def _connected_clients(clients):
    return [client for client in clients if client.connected]

def _create_data_writers(event_push_client, clients):
    data_writers_dict = dict()

    connected_clients_by_node = list()
    disconnected_clients_by_node = list()

    for node_name, client in zip(_node_names, clients):
        if client.connected:
            connected_clients_by_node.append((node_name, client))
        else:
            disconnected_clients_by_node.append((node_name, client))

    if len(connected_clients_by_node) < _min_connected_clients:
        raise exc.HTTPServiceUnavailable("Too few connected writers %s" % (
            len(connected_clients_by_node),
        ))

    connected_clients = list()
    for node_name, client in connected_clients_by_node:
        connected_clients.append(client)
        assert node_name not in data_writers_dict, connected_clients_by_node
        data_writers_dict[node_name] = DataWriter(node_name, client)
    
    for node_name, client in disconnected_clients_by_node:
        backup_clients = random.sample(connected_clients, _handoff_count)
        event_push_client.info(
            "handoff-start",
            "start handoff of %s to %s %s",
            start_time=time.time(),
            disconnected_node=node_name,
            backup_nodes=[b.server_node_name for b in backup_clients],
        )
        assert backup_clients[0] != backup_clients[1]
        data_writer_handoff_client = DataWriterHandoffClient(
            client.server_node_name,
            backup_clients
        )
        assert node_name not in data_writers_dict, data_writers_dict
        data_writers_dict[node_name] = DataWriter(
            node_name, data_writer_handoff_client
        )

    # 2011-05-27 dougfort -- the data-writers list must be in 
    # the same order as _node_names, because that's the order that
    # segment numbers get defined in
    return [data_writers_dict[node_name] for node_name in _node_names]

class Application(object):
    def __init__(
        self, 
        central_connection,
        node_local_connection,
        data_writer_clients, 
        data_readers,
        authenticator, 
        accounting_client,
        event_push_client
    ):
        self._log = logging.getLogger("Application")
        self._central_connection = central_connection
        self._node_local_connection = node_local_connection
        self._data_writer_clients = data_writer_clients
        self.data_readers = data_readers
        self._authenticator = authenticator
        self.accounting_client = accounting_client
        self._event_push_client = event_push_client

        self._cluster_row = get_cluster_row(self._central_connection)

        self._dispatch_table = {
            action_list_collections     : self._list_collections,
            action_create_collection    : self._create_collection,
            action_delete_collection    : self._delete_collection,
            action_space_usage          : self._collection_space_usage,
            action_archive_key          : self._archive_key,
            action_list_keys            : self._list_keys,
            action_retrieve_key         : self._retrieve_key,
            action_delete_key           : self._delete_key,
            action_head_key             : self._head_key,
        }

    @wsgify
    def __call__(self, req):

        result = parse_url(req.method, req.url)
        if result is None:
            self._log.error("Unparseable URL: %r" % (req.url, ))
            raise exc.HTTPNotFound(req.url)

        action_tag, match_object = result
        try:
            return self._dispatch_table[action_tag](req, match_object)
        except exc.HTTPException, instance:
            self._log.error("%s %s %s %r" % (
                instance.__class__.__name__, 
                instance, 
                action_tag,
                req.url
            ))
            raise
        except Exception, instance:
            self._log.exception("%s" % (req.url, ))
            self._event_push_client.exception(
                "unhandled_exception",
                str(instance),
                exctype=instance.__class__.__name__
            )
            raise

    def _list_collections(self, req, match_object):
        username = match_object.group("username")
        self._log.debug("_list_collections %r" % (username, ))

        authenticated = self._authenticator.authenticate(
            self._central_connection,
            username,
            req
        )
        if not authenticated:
            raise exc.HTTPUnauthorized()

        try:
            collections = list_collections(
                self._central_connection,
                username
            )
        except Exception, instance:
            self._log.error("%r error listing collections %s" % (
                username, instance,
            ))
            raise exc.HTTPServiceUnavailable(str(instance))

        # json won't dump datetime
        json_collections = [(n, t.isoformat()) for (n, t) in collections]

        response = Response(content_type='text/plain', charset='utf8')
        response.body_file.write(json.dumps(json_collections))

        return response

    def _create_collection(self, req, match_object):
        username = match_object.group("username")
        collection_name = match_object.group("collection_name")

        self._log.debug("_create_collection: %s name = %r" % (
            username,
            collection_name,
        ))

        authenticated = self._authenticator.authenticate(
            self._central_connection,
            username,
            req
        )
        if not authenticated:
            raise exc.HTTPUnauthorized()

        try:
            create_collection(
                self._central_connection, 
                username,
                collection_name
            )
        except Exception, instance:
            self._log.error("%s error adding collection %r %s" % (
                username, 
                collection_name, 
                instance,
            ))
            self._central_connection.rollback()
            raise exc.HTTPServiceUnavailable(str(instance))
        else:
            self._central_connection.commit()

        return Response('OK')

    def _delete_collection(self, req, match_object):
        username = match_object.group("username")
        collection_name = match_object.group("collection_name")

        self._log.debug("_delete_collection: %r %r" % (
            username, collection_name, 
        ))

        authenticated = self._authenticator.authenticate(
            self._central_connection,
            username,
            req
        )
        if not authenticated:
            raise exc.HTTPUnauthorized()

        # you can't delete your default collection
        default_collection_name = compute_default_collection_name(username)
        if collection_name == default_collection_name:
            raise exc.HTTPForbidden("Can't delete default collection %r" % (
                collection_name,
            ))

        # TODO: can't delete a collection that contains keys
        try:
            delete_collection(self._central_connection, collection_name)
        except Exception, instance:
            self._log.error("%r %r error deleting collection %s" % (
                username, collection_name, instance,
            ))
            self._central_connection.rollback()
            raise exc.HTTPServiceUnavailable(str(instance))
        else:
            self._central_connection.commit()

        return Response('OK')

    def _collection_space_usage(self, req, match_object):
        username = match_object.group("username")
        collection_name = match_object.group("collection_name")

        self._log.debug("_collection_space_usage: %r %r" % (
            username, collection_name
        ))

        authenticated = self._authenticator.authenticate(
            self._central_connection,
            username,
            req
        )
        if not authenticated:
            raise exc.HTTPUnauthorized()

        collection_id = get_collection_id(
            self._central_connection, collection_name
        )        
        if collection_id is None:
            raise exc.HTTPNotFound(collection_name)

        getter = SpaceUsageGetter(self.accounting_client)
        try:
            usage = getter.get_space_usage(collection_id, _reply_timeout)
        except (SpaceAccountingServerDownError, SpaceUsageFailedError), e:
            raise exc.HTTPServiceUnavailable(str(e))

        return Response(json.dumps(usage))

    def _archive_key(self, req, match_object):
        collection_name = match_object.group("collection_name")
        key = match_object.group("key")

        try:
            collection_entry = get_username_and_collection_id(
                self._central_connection, collection_name
            )
        except Exception, instance:
            self._log.error("%s" % (instance, ))
            raise exc.HTTPBadRequest()
            
        authenticated = self._authenticator.authenticate(
            self._central_connection,
            collection_entry.username,
            req
        )
        if not authenticated:
            raise exc.HTTPUnauthorized()

        try:
            key = urllib.unquote_plus(key)
            key = key.decode("utf-8")
        except Exception, instance:
            self._log.error('unable to prepare key %r %s' % (
                key, instance
            ))
            raise exc.HTTPServiceUnavailable(str(instance))

        start_time = time.time()
        description = \
                "archive: collection=(%s)%r customer=%r key=%r, size=%s" % (
            collection_entry.collection_id,
            collection_entry.collection_name,
            collection_entry.username,
            key, 
            req.content_length
        )
        self._log.debug(description)

        if req.content_length <= 0:
            raise exc.HTTPForbidden(
                "cannot archive: content_length = %s" % (req.content_length, )
            ) 

        meta_dict = _build_meta_dict(req.GET)

        data_writers = _create_data_writers(
            self._event_push_client,
            self._data_writer_clients
        ) 
        timestamp = create_timestamp()
        archiver = Archiver(
            data_writers,
            collection_entry.collection_id,
            key,
            timestamp,
            meta_dict
        )
        segmenter = ZfecSegmenter(
            8,
            len(data_writers)
        )
        file_adler32 = zlib.adler32('')
        file_md5 = hashlib.md5()
        file_size = 0
        segments = None
        try:
            for slice_item in DataSlicer(req.body_file,
                                    _slice_size,
                                    req.content_length):
                if segments:
                    archiver.archive_slice(
                        segments,
                        _reply_timeout
                    )
                    segments = None
                file_adler32 = zlib.adler32(slice_item, file_adler32)
                file_md5.update(slice_item)
                file_size += len(slice_item)
                segments = segmenter.encode(slice_item)
            if not segments:
                segments = segmenter.encode('')
            archiver.archive_final(
                file_size,
                file_adler32,
                file_md5.digest(),
                segments,
                _reply_timeout
            )
        except ArchiveFailedError, instance:
            self._event_push_client.error(
                "archived-failed-error",
                "%s: %s" % (description, instance, )
            )
            self._log.error("archive failed: %s %s" % (
                description, instance, 
            ))
            raise exc.HTTPInternalServerError(str(instance))
        
        end_time = time.time()

        self.accounting_client.added(
            collection_entry.collection_id,
            timestamp,
            file_size
        )

        self._event_push_client.info(
            "archive-stats",
            description,
            start_time=start_time,
            end_time=end_time,
            bytes_archived=req.content_length
        )

        return Response('OK')

    def _list_keys(self, req, match_object):
        collection_name = match_object.group("collection_name")

        try:
            collection_entry = get_username_and_collection_id(
                self._central_connection, collection_name
            )
        except Exception, instance:
            self._log.error("%s" % (instance, ))
            raise exc.HTTPBadRequest()
            
        authenticated = self._authenticator.authenticate(
            self._central_connection,
            collection_entry.username,
            req
        )
        if not authenticated:
            raise exc.HTTPUnauthorized()

        try:
            prefix = match_object.group("prefix")
            prefix = urllib.unquote_plus(prefix)
            prefix = prefix.decode("utf-8")
        except IndexError:
            prefix = u""
        except Exception, instance:
            raise exc.HTTPServiceUnavailable(str(instance))

        self._log.debug(
            "_list_keys: collection = (%s) username = %r %r prefix = '%s'" % (
                collection_entry.collection_id,
                collection_entry.collection_name,
                collection_entry.username,
                prefix
            )
        )
        matcher = Listmatcher(self._node_local_connection)
        keys = matcher.listmatch(
            collection_entry.collection_id, prefix, _reply_timeout
        )
        response = Response(content_type='text/plain', charset='utf8')
        response.body_file.write(json.dumps(keys))
        return response

    def _retrieve_key(self, req, match_object):
        collection_name = match_object.group("collection_name")
        key = match_object.group("key")

        try:
            collection_entry = get_username_and_collection_id(
                self._central_connection, collection_name
            )
        except Exception, instance:
            self._log.error("%s" % (instance, ))
            raise exc.HTTPBadRequest()
            
        authenticated = self._authenticator.authenticate(
            self._central_connection,
            collection_entry.username,
            req
        )
        if not authenticated:
            raise exc.HTTPUnauthorized()

        try:
            key = urllib.unquote_plus(key)
            key = key.decode("utf-8")
        except Exception, instance:
            raise exc.HTTPServiceUnavailable(str(instance))

        description = "retrieve: collection=(%s)%r customer=%r key=%r" % (
            collection_entry.collection_id,
            collection_entry.collection_name,
            collection_entry.username,
            key
        )
        self._log.debug(description)

        start_time = time.time()
        connected_data_readers = _connected_clients(self.data_readers)

        if len(connected_data_readers) < _min_connected_clients:
            raise exc.HTTPServiceUnavailable("Too few connected readers %s" % (
                len(connected_data_readers),
            ))

        segmenter = ZfecSegmenter(
            _min_segments,
            _max_segments)
        retriever = Retriever(
            self._node_local_connection,
            self.data_readers,
            collection_entry.collection_id,
            key,
            _min_segments
        )

        retrieved = retriever.retrieve(_reply_timeout)

        try:
            first_segments = retrieved.next()
        except RetrieveFailedError, instance:
            self._log.error("retrieve failed: %s %s" % (
                description, instance,
            ))
            self._event_push_client.error(
                "retrieve-failed",
                "%s: %s" % (description, instance, )
            )
            return exc.HTTPNotFound(str(instance))

        def app_iter():
            sent = 0
            try:
                for segments in chain([first_segments], retrieved):
                    data = segmenter.decode(segments.values())
                    sent += len(data)
                    yield data
            except RetrieveFailedError, instance:
                self._event_push_client.error(
                    "retrieve-failed",
                    "%s: %s" % (description, instance, )
                )
                self._log.error('retrieve failed: %s %s' % (
                    description, instance
                ))
                raise exc.HTTPInternalServerError(str(instance))

            end_time = time.time()

            self.accounting_client.retrieved(
                collection_entry.collection_id,
                create_timestamp(),
                sent
            )

            self._event_push_client.info(
                "retrieve-stats",
                description,
                start_time=start_time,
                end_time=end_time,
                bytes_retrieved=sent
            )

        return Response(app_iter=app_iter())

    def _delete_key(self, req, match_object):
        collection_name = match_object.group("collection_name")
        key = match_object.group("key")

        try:
            collection_entry = get_username_and_collection_id(
                self._central_connection, collection_name
            )
        except Exception, instance:
            self._log.error("%s" % (instance, ))
            raise exc.HTTPBadRequest()
            
        authenticated = self._authenticator.authenticate(
            self._central_connection,
            collection_entry.username,
            req
        )
        if not authenticated:
            raise exc.HTTPUnauthorized()

        try:
            key = urllib.unquote_plus(key)
            key = key.decode("utf-8")
        except Exception, instance:
            raise exc.HTTPServiceUnavailable(str(instance))

        self._log.debug(
            "_delete_key: collection = (%s) %r customer = %r key = %r" % (
                collection_entry.collection_id,
                collection_entry.collection_name,
                collection_entry.username,
                key,
        ))
        data_writers = _create_data_writers(
            self._event_push_client,
            self._data_writer_clients
        )

        timestamp = create_timestamp()

        destroyer = Destroyer(
            self._node_local_connection,
            data_writers,
            collection_entry.collection_id,
            key,
            timestamp
        )

        try:
            size_deleted = destroyer.destroy(_reply_timeout)
        except DestroyFailedError, e:            
            raise exc.HTTPInternalServerError(str(e))

        self.accounting_client.removed(
            collection_entry.collection_id,
            timestamp,
            size_deleted
        )
        return Response('OK')

    def _head_key(self, req, match_object):
        collection_name = match_object.group("collection_name")
        key = match_object.group("key")

        try:
            collection_entry = get_username_and_collection_id(
                self._central_connection, collection_name
            )
        except Exception, instance:
            self._log.error("%s" % (instance, ))
            raise exc.HTTPBadRequest()
            
        authenticated = self._authenticator.authenticate(
            self._central_connection,
            collection_entry.username,
            req
        )
        if not authenticated:
            raise exc.HTTPUnauthorized()

        try:
            key = urllib.unquote_plus(key)
            key = key.decode("utf-8")
        except Exception, instance:
            raise exc.HTTPServiceUnavailable(str(instance))

        self._log.debug(
            "head_key: collection = (%s) %r username = %r key = %r" % (
            collection_entry.collection_id, 
            collection_entry.collection_name,
            collection_entry.username,
            key
        ))

        getter = StatGetter(self._node_local_connection)
        file_info = getter.stat(
            collection_entry.collection_id, key, _reply_timeout
        )
        if file_info is None or file_info.file_tombstone:
            raise exc.HTTPNotFound("Not Found: %r" % (key, ))

        response = Response(status=200, content_type=None)
        response.content_length = file_info.file_size 
        response.content_md5 = b64encode(file_info.file_hash)

        return response

#    @routes.add(r'/data/(.+)$', action="get_meta")
#    def get_meta(self, collection_entry, req, key):
#        try:
#            key = urllib.unquote_plus(key)
#            key = key.decode("utf-8")
#        except Exception, instance:
#            self._log.error('unable to prepare key %r %s' % (
#                key, instance
#            ))
#            raise exc.HTTPServiceUnavailable(str(instance))
#
#        meta_value = get_meta(
#            self._node_local_connection,
#            collection_entry.collection_id,
#            key,
#            req.GET["meta_key"]
#        )
#
#        if meta_value is None:
#            raise exc.HTTPNotFound(req.GET["meta_key"])
#
#        response = Response(content_type='text/plain', charset='utf8')
#        response.body_file.write(meta_value)
#
#        return response
#
#    @routes.add(r'/data/(.+)$', action="list_meta")
#    def list_meta(self, collection_entry, _req, key):
#        try:
#            key = urllib.unquote_plus(key)
#            key = key.decode("utf-8")
#        except Exception, instance:
#            self._log.error('unable to prepare key %r %s' % (
#                key, instance
#            ))
#            raise exc.HTTPServiceUnavailable(str(instance))
#
#        meta_value = list_meta(
#            self._node_local_connection,
#            collection_entry.collection_id,
#            key
#        )
#
#        response = Response(content_type='text/plain', charset='utf8')
#        response.body_file.write(json.dumps(meta_value))
#
#        return response
#
#    @routes.add(r"/list_conjoined_archives")
#    def list_conjoined_archives(self, collection_entry, _req):
#        conjoined_value = list_conjoined_archives(
#            self._central_connection,
#            collection_entry.collection_id,
#        )
#
#        response = Response(content_type='text/plain', charset='utf8')
#        response.body_file.write(json.dumps(conjoined_value))
#
#        return response
#
#    @routes.add(r'/data/(.+)$', action="start_conjoined_archive")
#    def start_conjoined_archive(self, collection_entry, _req, key):
#        try:
#            key = urllib.unquote_plus(key)
#            key = key.decode("utf-8")
#        except Exception, instance:
#            self._log.error('unable to prepare key %r %s' % (
#                key, instance
#            ))
#            raise exc.HTTPServiceUnavailable(str(instance))
#
#        conjoined_identifier = start_conjoined_archive(
#            self._central_connection,
#            collection_entry.collection_id,
#            key
#        )
#
#        response = Response(content_type='text/plain', charset='utf8')
#        response.body_file.write(conjoined_identifier)
#
#        return response
#