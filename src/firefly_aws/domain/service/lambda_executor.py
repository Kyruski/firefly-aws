#  Copyright (c) 2020 JD Williams
#
#  This file is part of Firefly, a Python SOA framework built by JD Williams. Firefly is free software; you can
#  redistribute it and/or modify it under the terms of the GNU General Public License as published by the
#  Free Software Foundation; either version 3 of the License, or (at your option) any later version.
#
#  Firefly is distributed in the hope that it will be useful, but WITHOUT ANY WARRANTY; without even the
#  implied warranty of MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the GNU General
#  Public License for more details. You should have received a copy of the GNU Lesser General Public
#  License along with this program.  If not, see <http://www.gnu.org/licenses/>.
#
#  You should have received a copy of the GNU General Public License along with Firefly. If not, see
#  <http://www.gnu.org/licenses/>.

from __future__ import annotations

import base64
import bz2
import inspect
import io
import json
import math
import os
import re
import signal
import uuid
from contextlib import contextmanager
from datetime import datetime
from typing import Union

import firefly as ff
from multipart import MultipartParser

import firefly_aws.domain as domain


STATUS_CODES = {
    'BadRequest': 400,
    'Unauthorized': 401,
    'Forbidden': 403,
    'NotFound': 404,
    'ApiError': 500,
}

ACCESS_CONTROL_HEADERS = {
    'Access-Control-Allow-Origin': '*',
    'Access-Control-Allow-Methods': 'GET, POST, PUT, DELETE, PATCH, OPTIONS',
    'Access-Control-Allow-Headers': 'Authorization, Accept, Accept-Language, Content-Language, Content-Type, '
                                    'Content-Range',
    'Access-Control-Expose-Headers': '*',
}

COGNITO_TRIGGERS = (
    'PreSignUp_SignUp', 'PreSignUp_AdminCreateUser', 'PostConfirmation_ConfirmSignUp',
    'PostConfirmation_ConfirmForgotPassword', 'PreAuthentication_Authentication', 'PostAuthentication_Authentication',
    'DefineAuthChallenge_Authentication', 'CreateAuthChallenge_Authentication',
    'VerifyAuthChallengeResponse_Authentication', 'TokenGeneration_HostedAuth', 'TokenGeneration_Authentication',
    'TokenGeneration_NewPasswordChallenge', 'TokenGeneration_AuthenticateDevice', 'TokenGeneration_RefreshTokens',
    'UserMigration_Authentication', 'UserMigration_ForgotPassword', 'CustomMessage_SignUp',
    'CustomMessage_AdminCreateUser', 'CustomMessage_ResendCode', 'CustomMessage_ForgotPassword',
    'CustomMessage_UpdateUserAttribute', 'CustomMessage_VerifyUserAttribute', 'CustomMessage_Authentication'
)


@contextmanager
def time_limit(seconds):
    def signal_handler(signum, frame):
        raise domain.LambdaTimedOut('Time limit exceeded')
    signal.signal(signal.SIGALRM, signal_handler)
    signal.alarm(seconds)
    try:
        yield
    finally:
        signal.alarm(0)


class LambdaExecutor(ff.DomainService, domain.ResourceNameAware):
    _serializer: ff.Serializer = None
    _message_factory: ff.MessageFactory = None
    _rest_router: ff.RestRouter = None
    _s3_client = None
    _s3_service: domain.S3Service = None
    _bucket: str = None
    _kernel: ff.Kernel = None
    _handle_error: domain.HandleError = None
    _store_large_payloads_in_s3: domain.StoreLargePayloadsInS3 = None
    _load_payload: domain.LoadPayload = None
    _execution_context: domain.ExecutionContext = None
    _configuration: ff.Configuration = None
    _context: str = None

    def __init__(self):
        self._version_matcher = re.compile(r'^/v(\d)')
        self._default_matcher = re.compile(r'^/api/')

    def run(self, event: dict, context):
        try:
            time = self._get_remaining_time(context)
            if time is not None:
                with time_limit(time):
                    return self._do_run(event, context)
            else:
                return self._do_run(event, context)
        except Exception as e:
            self._handle_error(e, event, context)
            raise e

    def _do_run(self, event: dict, context):
        self.debug('Event: %s', event)
        self.debug('Context: %s', context)

        self._kernel.reset()

        self._execution_context.event = event
        self._execution_context.context = context

        if 'requestContext' in event and 'http' in event['requestContext']:
            self.info('HTTP request')
            return self._handle_http_event(event)

        if 'Records' in event and event['Records'][0].get('eventSource') in ('aws:sqs', 'aws:kinesis'):
            self.info('Async message')
            return self._handle_async_event(event)

        message = False
        aws_message = False
        if self._is_cognito_trigger_event(event):
            aws_message = True
            message = self._generate_cognito_trigger_messages(event)
            if message is False:
                self.debug('Passing through cognito trigger event')
                return event

        if message is False:
            message = self._serializer.deserialize(json.dumps(event))
        if isinstance(message, ff.Command):
            try:
                return self._serializer.deserialize(
                    self._store_large_payloads_in_s3(
                        self._serializer.serialize(self.invoke(message)),
                        name=message.__class__.__name__,
                        type_='command',
                        context=message.get_context(),
                        id_=getattr(message, '_id', str(uuid.uuid4()))
                    )
                )
            except ff.ConfigurationError:
                if aws_message is True:
                    return event
                raise
        elif isinstance(message, ff.Query):
            print('Handling query from within DC')
            response = self.request(message)
            print("Got response: ", str(response))
            return self._serializer.deserialize(
                self._store_large_payloads_in_s3(
                    self._serializer.serialize(response),
                    name=message.__class__.__name__,
                    type_='query',
                    context=message.get_context(),
                    id_=getattr(message, '_id', str(uuid.uuid4()))
                )
            )

        return {
            'statusCode': 200,
            'headers': ACCESS_CONTROL_HEADERS,
            'body': '',
            'isBase64Encoded': False,
        }

    def _handle_http_event(self, event: dict):
        route = self._default_matcher.sub('/', event['rawPath'])
        match = self._version_matcher.match(route)
        if match is not None and len(match.groups()) > 0:
            os.environ['API_VERSION'] = match.groups()[0]
        else:
            os.environ['API_VERSION'] = '1'

        route = self._version_matcher.sub('', route)
        method = event['requestContext']['http']['method']

        if method.lower() == 'options':
            return {
                'statusCode': 200,
                'headers': ACCESS_CONTROL_HEADERS,
            }

        body = None
        if 'body' in event:
            for k, v in event['headers'].items():
                if k.lower() == 'content-type':
                    if 'application/json' in v.lower():
                        body = self._serializer.deserialize(event['body'])
                    elif 'multipart/form-data' in v.lower():
                        body = self._parse_multipart(v, event['body'])
                    else:
                        body = event['body']
            if body is None:
                body = self._serializer.deserialize(event['body'])

        try:
            self.info(f'Trying to match route: "{method} {route}"')
            endpoint, params = self._rest_router.match(route, method)
            if not endpoint:
                return {
                    'statusCode': 404,
                    'headers': ACCESS_CONTROL_HEADERS,
                    'body': None,
                    'isBase64Encoded': False,
                }

            if endpoint.message is not None:
                message_name = endpoint.message if isinstance(endpoint.message, str) else endpoint.message.get_fqn()
            else:
                message_name = endpoint.service
                if inspect.isclass(message_name):
                    message_name = message_name.get_fqn()
            self.info(f'Matched route')

            if 'queryStringParameters' in event:
                params.update(event['queryStringParameters'])

            self._kernel.http_request = {
                'headers': event['headers'],
            }
            self._kernel.secured = endpoint.secured
            self._kernel.required_scopes = endpoint.scopes
            self._kernel.user = ff.User()

            try:
                if method.lower() == 'get':
                    return self._handle_http_response(self.request(message_name, data=params))
                else:
                    if body is not None:
                        if isinstance(body, dict):
                            params.update(body)
                        else:
                            params['body'] = body
                    return self._handle_http_response(self.invoke(message_name, params))
            except ff.UnauthenticatedError:
                self.info('Unauthenticated')
                return {
                    'statusCode': 403,
                    'headers': ACCESS_CONTROL_HEADERS,
                    'body': None,
                    'isBase64Encoded': False,
                }
            except (ff.UnauthorizedError, ff.Unauthorized):
                self.info('Unauthorized')
                return {
                    'statusCode': 401,
                    'headers': ACCESS_CONTROL_HEADERS,
                    'body': None,
                    'isBase64Encoded': False,
                }
            except ff.ApiError as e:
                return self._handle_http_response(str(e), status_code=STATUS_CODES[e.__class__.__name__])

        except TypeError:
            pass

    def _parse_multipart(self, header: str, body: str):
        boundary = None
        parts = header.split(';')
        for part in parts:
            if 'boundary=' in part:
                boundary = part.split('=')[-1]

        self.info('Boundary: %s', boundary)
        ret = {}
        body = base64.b64decode(body)
        parser = MultipartParser(io.BytesIO(body), boundary)
        for part in parser:
            if part.file and part.filename is not None:
                ret[part.name] = ff.File(
                    name=part.filename,
                    content=part.raw,
                    content_type=part.content_type
                )
            else:
                ret[part.name] = part.value

        return ret

    def _handle_http_response(self, response: any, status_code: int = 200, headers: dict = None):
        headers = headers or {}
        cookies = None
        if isinstance(response, ff.Envelope):
            if response.get_range() is not None:
                range_ = response.get_range()
                if range_['upper'] > range_['total']:
                    range_['upper'] = range_['total']
                headers['content-range'] = f'{range_["lower"]}-{range_["upper"]}/{range_["total"]}'
                if 'unit' in range_:
                    headers['content-range'] = f'{range_["unit"]} {headers["content-range"]}'
                status_code = 206
            if 'location' in response.headers:
                status_code = 303
                headers['location'] = response.headers['location']
            body = self._serializer.serialize(response.unwrap())
            cookies = response.get_cookies()
        else:
            body = self._serializer.serialize(response)
        headers.update(ACCESS_CONTROL_HEADERS)
        ret = {
            'statusCode': status_code,
            'headers': headers,
            'body': body,
            'isBase64Encoded': False,
        }
        if cookies:
            print('WE GOT THEM COOKIES')
            cookies = [self._make_cookie_from_dict(c) for c in cookies]
            ret['multiValueHeaders'] = {"Set-Cookie": cookies},

        print('WE GOT RET!@#$@%#$%!!!!!!!$@%!', ret)
        if len(body) > 6_000_000:
            download_url = self._s3_service.store_download(body, apply_compression=False)
            s3_download_domain = self._configuration.contexts[self._context].get('s3_download_domain')
            if s3_download_domain is not None:
                parts = download_url.split('/')
                download_url = f'https://{s3_download_domain}/{"/".join(parts[3:])}'

            ret['body'] = json.dumps({
                'location': download_url
            })
            ret['statusCode'] = 303
            ret['headers']['Location'] = download_url

        print('ANDNDNDNDNDNDNDNDND', ret)
        self.info(f'Proxy Response: %s', ret)
        return ret

    def _make_cookie_from_dict(self, cookie: Union[str, dict]):
        if isinstance(cookie, str):
            return cookie
        name = cookie['name']
        value = cookie['value']
        cookie_str = f'{name}={value}'
        if cookie.get('max_age'):
            max_age = cookie['max_age']
            cookie_str += f'; Max-Age={max_age}'
        if cookie.get('http_only'):
            cookie_str += '; HttpOnly'
        if cookie.get('expires'):
            expires = cookie['expires']
            cookie_str += f'; Expires={expires}'
        if cookie.get('domain'):
            domain = cookie['domain']
            cookie_str += f'; Domain={domain}'
        if cookie.get('path'):
            path = cookie['path']
            cookie_str += f'; Path={path}'
        if cookie.get('samesite'):
            samesite = cookie['samesite']
            cookie_str += f'; SameSite={samesite}'
        if cookie.get('secure'):
            cookie_str += f'; Secure'
        return cookie_str

    def _handle_async_event(self, event: dict):
        for record in event['Records']:
            if 'kinesis' in record:
                body = self._serializer.deserialize(record['kinesis']['data'])
            else:
                body = self._serializer.deserialize(record['body'])

            try:
                message: Union[ff.Event, dict] = self._serializer.deserialize(body['Message'])
            except KeyError:
                message = self._serializer.deserialize(body)
            except TypeError:
                message = body

            if (isinstance(message, dict) and 'PAYLOAD_KEY' in message) or \
                    (isinstance(message, ff.Message) and hasattr(message, 'PAYLOAD_KEY')):
                context = self._configuration.contexts['firefly_aws']
                function_name = self._lambda_function_name(self._context, 'Async')
                # If we're using adaptive memory and this is the router, we don't want to load the entire message.
                if context.get('memory_async') != 'adaptive' or \
                        not self._execution_context.context or \
                        self._execution_context.context.function_name != function_name:
                    try:
                        payload_key = message['PAYLOAD_KEY'] if isinstance(message, dict) else message.PAYLOAD_KEY
                        self.info('Payload key: %s', payload_key)
                        message = self._load_payload(payload_key)
                    except Exception as e:
                        self.nack_message(record)
                        self.error(e)
                        continue
                else:
                    message = self._serializer.deserialize(self._serializer.serialize(message))

            if message is None:
                self.info('Got a null message')
                return

            if 'kinesis' in record:
                message = self._message_factory.command('firefly_aws.UpdateResourceSettings', message)

            try:
                message.headers['external'] = True
            except AttributeError:
                self.info('message is not of type Message')

            if isinstance(message, ff.Command):
                self.invoke(message)
            else:
                self.dispatch(message)
        if len(event['Records']) == 1:
            self.complete_handshake(event['Records'][0])
        else:
            self.complete_batch_handshake(event['Records'])

    @staticmethod
    def _is_cognito_trigger_event(event: dict):
        return 'triggerSource' in event

    def _generate_cognito_trigger_messages(self, event: dict):
        if event['triggerSource'] in COGNITO_TRIGGERS:
            return self._message_factory.command(
                f'{os.environ.get("CONTEXT", "firefly_aws")}.{event["triggerSource"]}',
                data={
                    'event': event
                }
            )
        return False

    @staticmethod
    def _get_remaining_time(context):
        if context is not None:
            return math.floor((context.get_remaining_time_in_millis() / 1000) * .9)

    def nack_message(self, record: dict):
        pass

    def complete_handshake(self, record: dict):
        pass

    def complete_batch_handshake(self, records: list):
        pass
