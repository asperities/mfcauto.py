"""Tools for handling network communication with MFC chat servers"""
# pylint: disable=logging-format-interpolation, no-else-return
import sys
import urllib.request
import json
import asyncio
import random
import struct
import traceback
from threading import RLock
from .event_emitter import EventEmitter
from .packet import Packet
from .constants import MAGIC, FCTYPE, FCCHAN, FCWOPT, FCL, FCLEVEL, STATE
from .model import Model
from .utils import log

__all__ = ['Client', 'SimpleClient']

class MFCProtocol(asyncio.Protocol):
    """asyncio.Protocol handler for MFC"""
    def __init__(self, loop, client):
        self.loop = loop
        self.client = client
        self.buffer = b""
    def connection_lost(self, exc):
        self.client.handle_disconnected()
        # if exc is None:
        #     # We lost our connection, but there was no exception.
        #     # Someone called client.disconnect()?
        #     pass
        # else:
        #     # There was an exception for abnormal termination...
        #     pass
    def data_received(self, data):
        self.buffer += data
        while True:
            try:
                pformat = ">iiiiiii"
                packet_size = struct.calcsize(pformat)
                if len(self.buffer) < packet_size:
                    break

                #unpacked_data looks like this: (magic, fctype, nfrom, nto, narg1, narg2, spayload)
                unpacked_data = struct.unpack(pformat, self.buffer[:packet_size])
                assert unpacked_data[0] == MAGIC
                spayload = unpacked_data[6]
                smessage = None
                if spayload > 0:
                    if len(self.buffer) < (packet_size+spayload):
                        break
                    smessage = struct.unpack("{}s".format(spayload),
                                             self.buffer[packet_size:packet_size+spayload])
                    smessage = smessage[0].decode('utf-8')
                    try:
                        smessage = json.loads(smessage)
                    except json.decoder.JSONDecodeError:
                        pass

                self.buffer = self.buffer[packet_size+spayload:]
                self.client.handle_packet_received(Packet(*unpacked_data[1:-1], smessage))
            except:
                ex = sys.exc_info()[0]
                log.critical("Unexpected exception: {}\n{}".format(ex, traceback.format_exc()))
                self.loop.stop()
                break

class Client(EventEmitter):
    """An MFC Client object"""
    userQueryLock = RLock()
    userQueryId = 20
    def __init__(self, loop, username='guest', password='guest'):
        self.loop = loop
        self.username = username
        self.password = password
        self.server_config = None
        self.transport = None
        self.protocol = None
        self.session_id = 0
        self.loop = asyncio.get_event_loop()
        self.keepalive = None
        self._completed_models = False
        self._completed_tags = False
        self.uid = None
        self._manual_disconnect = False
        self._logged_in = False
        self.stream_cxid = None
        self.stream_password = None
        self.stream_vidctx = None
        super().__init__()
    def handle_packet_received(self, packet):
        """Internal handler invoked when a full packet is received by the protocol"""
        log.debug(packet)
        self._process_packet(packet)
        self.emit(packet.fctype, packet)
        self.emit(FCTYPE.ANY, packet)
    def _process_packet(self, packet):
        """Merges the given packet into our global state"""
        fctype = packet.fctype
        if fctype == FCTYPE.LOGIN:
            if packet.narg1 != 0:
                log.info("Login failed for user '{}' password '{}'"
                         .format(self.username, self.password))
                raise Exception("Login failed")
            else:
                self.session_id = packet.nto
                self.uid = packet.narg2
                self.username = packet.smessage
                log.info("Login handshake completed. Logged in as '{}' with sessionId {}"
                         .format(self.username, self.session_id))
        elif fctype in (FCTYPE.DETAILS, FCTYPE.ROOMHELPER, FCTYPE.SESSIONSTATE,
                        FCTYPE.ADDFRIEND, FCTYPE.ADDIGNORE, FCTYPE.CMESG,
                        FCTYPE.PMESG, FCTYPE.TXPROFILE, FCTYPE.USERNAMELOOKUP,
                        FCTYPE.MYCAMSTATE, FCTYPE.MYWEBCAM):
            if not ((fctype == FCTYPE.DETAILS and packet.nfrom == FCTYPE.TOKENINC)
                    or (fctype == FCTYPE.ROOMHELPER and packet.narg2 < 100)
                    or (fctype == FCTYPE.JOINCHAN and packet.narg2 == FCCHAN.PART)):
                if isinstance(packet.smessage, dict):
                    user_level = packet.smessage.setdefault("lv", None)
                    user_id = packet.smessage.setdefault("uid", None)
                    if user_id is None:
                        user_id = packet.aboutmodel.uid
                    if (user_id != None and user_id != -1
                            and (user_level != None or user_level == 4)):
                        possiblemodel = Model.get_model(user_id, user_level == 4)
                        if possiblemodel != None:
                            possiblemodel.merge(packet.smessage)
        elif fctype == FCTYPE.TAGS:
            if isinstance(packet.smessage, dict):
                # Sometimes TAGS are so long that they're malformed JSON.
                # For now, just ignore those cases.
                for key, value in packet.smessage.items():
                    possible_model = Model.get_model(int(key))
                    if possible_model != None:
                        possible_model.merge_tags(value)
        elif fctype == FCTYPE.BOOKMARKS:
            if "bookmarks" in packet.smessage and isinstance(packet.smessage["bookmarks"], list):
                for bookmark in packet.smessage["bookmarks"]:
                    possible_model = Model.get_model(bookmark["uid"])
                    if possible_model != None:
                        possible_model.merge(bookmark)
        elif fctype == FCTYPE.METRICS:
            # Note that after MFC server updates on 2017-04-18, Metrics packets are rarely,
            # or possibly never, sent
            pass
        elif fctype == FCTYPE.EXTDATA:
            if packet.nto == self.session_id and packet.narg2 == FCWOPT.REDIS_JSON:
                self._handle_extdata(packet.smessage)
        elif fctype == FCTYPE.MANAGELIST:
            if packet.narg2 > 0 and "rdata" in packet.smessage:
                rdata = self._process_list(packet.smessage["rdata"])
                ntype = packet.narg2
                if ntype in (FCL.ROOMMATES, FCL.CAMS, FCL.FRIENDS, FCL.IGNORES) and isinstance(rdata, list):
                    for payload in rdata:
                        possible_model = Model.get_model(payload["uid"], payload["lv"] == FCLEVEL.MODEL)
                        if possible_model != None:
                            possible_model.merge(payload)
                    if ntype == FCL.CAMS and not self._completed_models:
                        self._completed_models = True
                        self.emit(FCTYPE.CLIENT_MODELSLOADED)
                elif ntype == FCL.TAGS and isinstance(rdata, dict):
                    for key, value in rdata.items():
                        possible_model = Model.get_model(key)
                        if possible_model != None:
                            possible_model.merge_tags(value)
                    if not self._completed_tags:
                        self._completed_tags = True
                        self.emit(FCTYPE.CLIENT_TAGSLOADED)
        elif fctype == FCTYPE.TKX:
            if "cxid" in packet.smessage and "tkx" in packet.smessage and "ctxenc" in packet.smessage:
                self.stream_cxid = packet.smessage["cxid"]
                self.stream_password = packet.smessage["tkx"]
                parts = packet.smessage["ctxenc"].split("/")
                self.stream_vidctx = parts[1] if len(parts) > 1 else packet.smessage["ctxenc"]
    def _get_servers(self):
        if self.server_config is None:
            with urllib.request.urlopen('http://www.myfreecams.com/_js/serverconfig.js') as req:
                self.server_config = json.loads(req.read().decode('utf-8'))
    def _ping_loop(self):
        self.tx_cmd(FCTYPE.NULL, 0, 0, 0)
        self.keepalive = self.loop.call_later(120, self._ping_loop)
    async def connect(self, login=True):
        """Connects to an MFC chat server and optionally logs in"""
        self._get_servers()
        selected_server = random.choice(self.server_config['chat_servers'])
        self._logged_in = login
        log.info("Connecting to MyFreeCams chat server {}...".format(selected_server))
        (self.transport, self.protocol) = await self.loop.create_connection(lambda: MFCProtocol(self.loop, self), '{}.myfreecams.com'.format(selected_server), 8100)
        if login:
            self.tx_cmd(FCTYPE.LOGIN, 0, 20071025, 0, "{}:{}".format(self.username, self.password))
            if self.keepalive is None:
                self.keepalive = self.loop.call_later(120, self._ping_loop)
        self.loop.call_soon(self.emit, FCTYPE.CLIENT_CONNECTED)
    def disconnect(self):
        """Disconnects from the MFC chat server and closes the underlying transport"""
        self._manual_disconnect = True
        self.transport.close()
    def handle_disconnected(self):
        """Handles disconnect events from the underlying MFCProtocol
        instance, reconnecting as needed"""
        if self.keepalive != None:
            self.keepalive.cancel()
            self.keepalive = None
        if self.password == "guest" and self.username.startswith("Guest"):
            self.username = "guest"
        self._completed_models = False
        self._completed_tags = False
        if not self._manual_disconnect:
            print("Disconnected from MyFreeCams.  Reconnecting in 30 seconds...")
            self.loop.call_later(30, lambda: asyncio.ensure_future(self.connect(self._logged_in)))
        else:
            self.loop.stop()
        self._manual_disconnect = False
        self.emit(FCTYPE.CLIENT_DISCONNECTED)
        Model.All.reset()
    def tx_cmd(self, fctype, nto, narg1, narg2, smsg=''):
        """Transmits a command back to the connected MFC chat server"""
        if not isinstance(fctype, FCTYPE):
            raise Exception("Please provide a valid FCTYPE")
        if smsg is None:
            smsg = ''
        data = struct.pack(">iiiiiii{}s".format(len(smsg)), MAGIC, fctype, self.session_id,
                           nto, narg1, narg2, len(smsg), smsg.encode())
        log.debug("TxCmd sending: {}".format(data))
        self.transport.write(data)
    def tx_packet(self, packet):
        """Transmits a packet back to the connected MFC chat server"""
        self.tx_cmd(packet.fctype, packet.nto, packet.narg1, packet.narg2, packet.smessage)
    def _handle_extdata(self, extdata):
        if extdata != None and "respkey" in extdata:
            url = "http://www.myfreecams.com/php/FcwExtResp.php?"
            for name in ["respkey", "type", "opts", "serv"]:
                if name in extdata:
                    url += "{}={}&".format(name, extdata.setdefault(name, None))
            with urllib.request.urlopen(url) as req:
                contents = json.loads(req.read().decode('utf-8'))
                packet = Packet(extdata["msg"]["type"], extdata["msg"]["from"],
                                extdata["msg"]["to"], extdata["msg"]["arg1"],
                                extdata["msg"]["arg2"], contents)
                self.handle_packet_received(packet)
    @staticmethod
    def _process_list(data):
        if isinstance(data, list) and len(data) > 0:
            result = []
            schema = data[0]
            schema_map = []
            for path1 in schema:
                if isinstance(path1, dict):
                    for key in path1:
                        for path2 in path1[key]:
                            schema_map.append([key, path2])
                elif isinstance(path1, str):
                    schema_map.append([path1])
            for record in data[1:]:
                if isinstance(record, list):
                    msg = {}
                    for i, item in enumerate(record):
                        path = schema_map[i]
                        if len(path) == 1:
                            msg[path[0]] = item
                        else:
                            msg.setdefault(path[0], {})[path[1]] = item
                    result.append(msg)
                elif isinstance(record, dict):
                    result.append(record)
            return result
        else:
            return data
    @staticmethod
    def touserid(uid):
        """Converts an id that might be a user id or room id to a user id"""
        if uid >= 1000000000:
            uid = uid - 1000000000
        elif uid >= 400000000:
            uid = uid - 400000000
        elif uid >= 300000000:
            uid = uid - 300000000
        elif uid >= 200000000:
            uid = uid - 200000000
        elif uid >= 100000000:
            uid = uid - 100000000
        return uid
    @staticmethod
    def toroomid(the_id):
        """Converts an id that might be a user id or room id to a room id"""
        if the_id < 100000000:
            the_id = the_id + 100000000
        return the_id
    def sendchat(self, the_id, msg):
        """Send chat to the given model's room"""
        the_id = Client.toroomid(the_id)
        self.tx_cmd(FCTYPE.CMESG, the_id, 0, 0, msg)
        #@TODO - Emote encoding
    def sendpm(self, the_id, msg):
        """Send pm to the given user/model"""
        the_id = Client.touserid(the_id)
        self.tx_cmd(FCTYPE.PMESG, the_id, 0, 0, msg)
        #@TODO - Emote encoding
    def joinroom(self, the_id):
        """Join the given model's room"""
        the_id = Client.toroomid(the_id)
        self.tx_cmd(FCTYPE.JOINCHAN, 0, the_id, FCCHAN.JOIN)
    def leaveroom(self, the_id):
        """Leave the given model's room"""
        the_id = Client.toroomid(the_id)
        self.tx_cmd(FCTYPE.JOINCHAN, 0, the_id, FCCHAN.PART)
    def query_user(self, user):
        """Query the servers for a given user's status and details. User may
        be a string name or integer id"""
        with Client.userQueryLock:
            future = asyncio.Future()
            query_id = Client.userQueryId
            Client.userQueryId += 1
            def handler(packet):
                """Handles the usernamelookup response for this query_user"""
                if packet.narg1 == query_id:
                    self.remove_listener(FCTYPE.USERNAMELOOKUP, handler)
                    if (not hasattr(packet, "smessage")) or not isinstance(packet.smessage, dict):
                        future.set_result(None) # User doesn't exist
                    else:
                        future.set_result(packet.smessage)
            self.on(FCTYPE.USERNAMELOOKUP, handler)
            if isinstance(user, int):
                self.tx_cmd(FCTYPE.USERNAMELOOKUP, 0, query_id, user)
            elif isinstance(user, str):
                self.tx_cmd(FCTYPE.USERNAMELOOKUP, 0, query_id, 0, user)
            else:
                raise Exception("Invalid Argument")
            return future
    def get_hls_url(self, model):
        if isinstance(model, int):
            model = Model.get_model(model)
        if "camserv" not in model.bestsession or self.server_config == None or model.bestsession["vs"] != STATE.FreeChat:
            return None
        camserv = model.bestsession["camserv"]
        camservstr = str(camserv)
        roomId = Client.toroomid(model.uid)
        if "ngvideo_servers" in self.server_config and camservstr in self.server_config["ngvideo_servers"]:
            videoserv = self.server_config["ngvideo_servers"][camservstr]
            return "https://{}.myfreecams.com:8444/x-hls/{}/{}/{}/{}/{}_{}_{}.m3u8".format(
                videoserv,
                self.stream_cxid,
                roomId,
                self.stream_password,
                self.stream_vidctx,
                roomprefix,
                model.bestsession.setdefault("phase", "a"),
                roomId
            )
        else:
            if model.bestsession.get('phase', None) == "a":
                roomprefix = "mfc_a"
            else:
                roomprefix = "mfc"

            videoserv = "video{}".format(camserv - 500)  # - 700
            return "https://{}.myfreecams.com:443/NxServer/ngrp:{}_{}.f4v_mobile/playlist.m3u8?nc={}".format(
                videoserv,
                roomprefix,
                roomId,
                str(random.random())  # .replace("0.","")
            )

class SimpleClient(Client):
    """An MFC Client object that maintains its own default event loop"""
    def __init__(self, username='guest', password='guest'):
        super().__init__(asyncio.get_event_loop(), username, password)
    def connect(self, login=True):
        """A blocking call that connects to MFC and begins processing the event loop"""
        self.loop.run_until_complete(super().connect(login))
        self.loop.run_forever()
        self.loop.close()
