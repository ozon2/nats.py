# Copyright 2021 The NATS Authors
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#

from abc import ABC, abstractmethod
from typing import Optional
from nats.js import api
from nats.js.errors import NotFoundError, BucketNotFoundError, KeyDeletedError, BadBucketError
from nats.js.headers import KV_EXPECTED_HDR, MSG_ROLLUP_HDR
from dataclasses import dataclass
import base64

KV_OP = "KV-Operation"
KV_DEL = "DEL"
KV_PURGE = "PURGE"
MSG_ROLLUP_SUBJECT = "sub"
MSG_ROLLUP_ALL = "all"


class KeyValue:
    """
    KeyValue uses the JetStream KeyValue functionality.

    .. note::
       This functionality is EXPERIMENTAL and may be changed in later releases.

    ::

        import asyncio
        import nats

        async def main():
            nc = await nats.connect()
            js = nc.jetstream()

            # Create a KV
            kv = await js.create_key_value(bucket='MY_KV')

            # Set and retrieve a value
            await kv.put('hello', b'world')
            entry = await kv.get('hello')
            print(f'KeyValue.Entry: key={entry.key}, value={entry.value}')
            # KeyValue.Entry: key=hello, value=world

            await nc.close()

        if __name__ == '__main__':
            asyncio.run(main())

    """
    @dataclass
    class Entry:
        """
        An entry from a KeyValue store in JetStream.
        """
        bucket: str
        key: str
        value: Optional[bytes]
        revision: int

    class BucketStatus:
        """
        BucketStatus is the status of a KeyValue bucket.
        """
        def __init__(self) -> None:
            self._nfo = None
            self._bucket = None

        @property
        def bucket(self):
            """
            name of the bucket the key value.
            """
            return self._bucket

        @property
        def values(self):
            """
            values returns the number of stored messages in the stream.
            """
            return self._nfo.state.messages

        @property
        def history(self):
            """
            history returns the max msgs per subject.
            """
            return self._nfo.config.max_msgs_per_subject

        @property
        def ttl(self):
            """
            ttl returns the max age in seconds.
            """
            return self._nfo.config.max_age / 1_000_000_000

        @property
        def stream_info(self):
            return self._nfo

        def __repr__(self) -> str:
            attrs = 'bucket={self.bucket} values={self.values} history={self.history} ttl={self.ttl}'
            return f"<KeyValue.{self.__class__.__name__}: {attrs}>"

    def __init__(self, name, stream, pre, js) -> None:
        self._name = name
        self._stream = stream
        self._pre = pre
        self._js = js

    async def get(self, key: str) -> Entry:
        """
        get returns the latest value for the key.
        """
        msg = await self._js.get_last_msg(self._stream, f"{self._pre}{key}")
        data = None
        if msg.data:
            data = base64.b64decode(msg.data)

        entry = KeyValue.Entry(
            bucket=self._name,
            key=key,
            value=data,
            revision=msg.seq,
        )

        # Check headers to see if deleted or purged.
        if msg.headers:
            op = msg.headers.get(KV_OP, None)
            if op == KV_DEL or op == KV_PURGE:
                raise KeyDeletedError(entry, op)

        return entry

    async def put(self, key: str, value: bytes) -> int:
        """
        put will place the new value for the key into the store
        and return the revision number.
        """
        pa = await self._js.publish(f"{self._pre}{key}", value)
        return pa.seq

    async def update(self, key: str, value: bytes, last: int) -> int:
        """
        update will update the value iff the latest revision matches.
        """
        hdrs = {}
        hdrs[KV_EXPECTED_HDR] = str(last)
        pa = await self._js.publish(f"{self._pre}{key}", value, headers=hdrs)
        return pa.sequence

    async def delete(self, key: str) -> bool:
        """
        delete will place a delete marker and remove all previous revisions.
        """
        hdrs = {}
        hdrs[KV_OP] = KV_DEL
        await self._js.publish(f"{self._pre}{key}", headers=hdrs)
        return True

    async def purge(self, key: str) -> bool:
        """
        purge will remove the key and all revisions.
        """
        hdrs = {}
        hdrs[KV_OP] = KV_PURGE
        hdrs[MSG_ROLLUP_HDR] = MSG_ROLLUP_SUBJECT
        await self._js.publish(f"{self._pre}{key}", headers=hdrs)
        return True

    async def status(self) -> BucketStatus:
        """
        status retrieves the status and configuration of a bucket.
        """
        info = await self._js.stream_info(self._stream)
        kvs = KeyValue.BucketStatus()
        kvs._nfo = info
        kvs._bucket = self._name
        return kvs


class KeyValueManager(ABC):
    """
    KeyValueManager includes methods to create KV stores in JetStream.
    """
    @abstractmethod
    async def stream_info(self, name: str):
        pass

    @abstractmethod
    async def add_stream(self, config: api.StreamConfig):
        pass

    @abstractmethod
    async def delete_stream(self, name: str):
        pass

    async def key_value(self, bucket: str):
        stream = f"KV_{bucket}"
        try:
            si = await self.stream_info(stream)
        except NotFoundError:
            raise BucketNotFoundError
        if si.config.max_msgs_per_subject < 1:
            raise BadBucketError

        return KeyValue(
            name=bucket,
            stream=stream,
            pre=f"$KV.{bucket}.",
            js=self,
        )

    async def create_key_value(self, **params) -> KeyValue:
        """
        create_key_value takes an api.KeyValueConfig and creates a KV in JetStream.
        """
        config = api.KeyValueConfig.loads(**params)
        if not config.history:
            config.history = 1
        if not config.replicas:
            config.replicas = 1
        if config.ttl:
            config.ttl = config.ttl * 1_000_000_000

        stream = api.StreamConfig(
            name=f"KV_{config.bucket}",
            description=None,
            subjects=[f"$KV.{config.bucket}.>"],
            max_msgs_per_subject=config.history,
            max_bytes=config.max_bytes,
            max_age=config.ttl,
            max_msg_size=config.max_value_size,
            storage=config.storage,
            num_replicas=config.replicas,
            allow_rollup_hdrs=True,
            deny_delete=True,
        )
        await self.add_stream(stream)

        return KeyValue(
            name=config.bucket,
            stream=stream.name,
            pre=f"$KV.{config.bucket}.",
            js=self,
        )

    async def delete_key_value(self, bucket: str):
        """
        delete_key_value deletes a JetStream KeyValue store by destroying
        the associated stream.
        """
        return await self.delete_stream(bucket)