from __future__ import annotations

import uuid

import firefly as ff


class StoreLargePayloadsInS3(ff.DomainService):
    _s3_client = None
    _serializer: ff.Serializer = None
    _bucket: str = None

    def __call__(self, payload: str, name: str, context: str, type_: str, id_: str):
        print('We have arguments: payload: ', type(payload), payload)
        print('We have arguments: name: ', type(name), name)
        print('We have arguments: context: ', type(context), context)
        print('We have arguments: type_: ', type(type_), type_)
        print('We have arguments: id_: ', type(id_), id_)
        if len(payload) > 64_000:
            key = f'tmp/{str(uuid.uuid1())}.json'
            self._s3_client.put_object(
                Body=payload,
                Bucket=self._bucket,
                Key=key
            )
            return self._serializer.serialize({
                'PAYLOAD_KEY': key,
                '_name': name,
                '_type': type_,
                '_context': context,
                '_id': id_,
            })

        return payload
