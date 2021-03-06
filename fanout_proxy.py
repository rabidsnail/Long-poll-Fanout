import tornado.web as web
import tornado.httpclient as http
from tornado.ioloop import IOLoop
from tornado.options import define, options
import re, struct, datetime, time
import http_utils
from functools import partial
from cStringIO import StringIO
from datetime import timedelta

define('auth_url',
    help='An endpoint that takes post requests with comma-separated lists of urls and returns either 200 indicating the client is allowed to access those urls or 403 otherwise')
define('port', default='5000')
define('default_polling_interval', default=5,
    help="How many seconds to wait between polling requests if no cache interval is supplied")

ioloop = IOLoop.instance()

def make_sse_response_blob(url, response):
    res = StringIO()
    res.write('event: %s\n' % url)
    lines = re.split(r'[\r\n]+', response.body)
    for line in lines:
        res.write('data: %s\n' % line)
    res.write('\r\n')
    return res.getvalue()

def make_binary_response_blob(url, response):
    res = StringIO()
    res.write(url)
    res.write('\n')
    res.write(struct.pack('>I', len(response.body)))
    res.write(response.body)
    return res.getvalue()

serializers = {
    'sse':make_sse_response_blob,
    'bin':make_binary_response_blob}

class ReferenceCounter(object):

    def __init__(self):
        self.mapping = {}

    def add(self, item):
        try:
            self.mapping[item] += 1
        except KeyError:
            self.mapping[item] = 1

    def remove(self, item):
        try:
            self.mapping[item] -= 1
            if self.mapping[item] < 1:
                del self.mapping[item]
        except KeyError:
            pass

    def get(self, item):
        return self.mapping.get(item, 0)

    def __contains__(self, item):
        return item in self.mapping

    def values(self):
        return self.mapping.keys()

class UrlMapping(object):

    def __init__(self):
        self.keys_to_clients = {}
        self.serializer_references = ReferenceCounter()

    def add(self, url, serializer, client):
        self.serializer_references.add(serializer)
        try:
            s = self.keys_to_clients[url]
        except KeyError:
            s = set([])
            self.keys_to_clients[url] = s
            self.fetch_url(url)

        s.add(client)

        return partial(self.remove_value, url, serializer)

    def remove_value(self, url, serializer, client):
        print 'remove %s' % url
        self.serializer_references.remove(serializer)
        v = self.keys_to_clients[url]
        v.remove(client)
        if not v:
            del self.keys_to_clients[url]

    def fetch_url(self, url):
        print url
        http.AsyncHTTPClient().fetch(url, partial(self.got_url, url))

    def got_url(self, url, response):
        refs = self.keys_to_clients.get(url)

        if not refs:
            return

        serial = dict((s, s(url, response)) for s in self.serializer_references.values())

        for client in refs:
            client.send_blob(serial[client.serializer])

        recur = partial(self.fetch_url, url)

        if 'Expires' in response.headers:
            try:
                interval = http_utils.http_date_to_epoch(response.headers['Expires'])
                print 'waiting until %d' % interval
                ioloop.add_timeout(interval, recur)
                return
            except ValueError:
                pass

        if 'Cache-Control' in response.headers:
            dt = http_utils.parse_cache_control(response.headers['Cache-Control'])
            if dt:
                print 'waiting %r' % dt
                ioloop.add_timeout(dt, recur)
                return

        ioloop.add_timeout(timedelta(seconds=options.default_polling_interval), recur)

url_mapping = UrlMapping()

class PersistentClientHandler(web.RequestHandler):

    @web.asynchronous
    def get(self, serializer, urls):
        self.urls = urls
        self.finish_callbacks = []
        try:
            self.serializer = serializers[serializer]
        except KeyError:
            self.send_error(400, 'Invalid serializer')
            self.finish()
            return

        headers = self.request.headers
        headers.add('X-Real-IP', self.request.remote_ip)
        request = http.HTTPRequest(
            options.auth_url,
            body=urls,
            method='POST',
            headers=headers)
        http.AsyncHTTPClient().fetch(request, self.got_auth_response)

    def got_auth_response(self, response):
        if response.code == 200:
            self.setup_connections()
        else:
            try:
                self.set_status(response.code)
            except ValueError:
                self.set_status(500)
            self.finish(response.body)

    def setup_connections(self):
        urls = self.urls.split(',')
        for url in urls:
            self.finish_callbacks.append(url_mapping.add(url, self.serializer, self))
        self.set_status(200)

    def send_blob(self, blob):
        self.write(blob)
        self.flush()

    def on_connection_close(self):
        for c in self.finish_callbacks:
            c(self)
        web.RequestHandler.on_connection_close(self)

app = web.Application([
    ('/(.*?)/(.*)', PersistentClientHandler)
], transforms=[])

if __name__ == '__main__':
    options.parse_command_line()
    assert options.auth_url, 'You must specify an auth_url'
    app.listen(options.port)
    ioloop.start()
