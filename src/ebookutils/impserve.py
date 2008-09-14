#!/usr/bin/env python

##
##  Copyright (c) 2008 by the people mentioned in the file AUTHORS.
##
##  This software is licensed under the terms mentioned in the file LICENSE.
##

import os, sys, re, imp, signal, socket, select, urlparse, urllib, shutil
import BaseHTTPServer, mimetypes, cgi, getopt

from os.path    import *
from ebookutils import __version__

################################################################ service URLs

LOCAL_DOMAIN    = ".ebooksystem.net"
BOOKLIST_PREFIX = "http://bookshelf.ebooksystem.net/bookshelf/default.asp?"
BOOK_PREFIX     = "http://bookshelf.ebooksystem.net/bookshelf/getbook?"
CONTENT_PREFIX  = "http://bookshelf.ebooksystem.net/content/"
REDIRECT_PREFIX = "http://register.ebooksystem.net/form/redirect.asp"
INDEX_FILES     = ['index.htm', 'index.html']
CONTENT_FILE    = re.compile('filename="?([^"]+)"?')
URL_CACHE_MAX   = 10

############################################################## ebook metadata

def get_ebook_info(name):
    """ get the details of the IMP book as a tuple """
    if not isfile(name):
        return None
    info = [name, getsize(name), getmtime(name)]
    f = open(name, 'rb')
    if f.read(10) != '\x00\x02BOOKDOUG':
        return None

    def cString(skip=0):
        result = ''
        while 1:
            data = f.read(1)
            if data == '\x00':
                if not skip: return result
                skip -= 1
                result, data = '', ''
            result += data

    f.read(38)
    info += [cString(), cString(), cString(1), cString(2)]
    f.close()
    return tuple(info)

def get_ebook_list(path, existing={}):
    """ get the list of ebooks under a given path (with optional caching) """

    if not isdir(path):
        return existing

    current = {}
    for file, info in existing.items():
        if not isfile(file):
            continue
        if getmtime(file) != info[2]:
            current[file] = get_ebook_info(file)
        else:
            current[file] = info

    for root, dirs, files in os.walk(abspath(path)):
        for name in files:
            if name.lower().endswith('.imp'):
                fname = join(root, name)
                if fname not in existing:
                    current[fname] = get_ebook_info(fname)
    return current

################################################################ misc helpers

def get_root():
    """ return the root directory to serve from """

    if 'IMPSERVE' in os.environ:
        return abspath(os.environ['IMPSERVE'])
    if isdir(expanduser("~/.impserve")):
        return abspath(expanduser("~/.impserve"))
    if sys.platform == 'win32' and \
            isdir(join(os.environ.get("APPDATA", ""), "impserve")):
        return abspath(join(os.environ.get("APPDATA", ""), "impserve"))
    if sys.argv[0]:
        return dirname(abspath(sys.argv[0]))
    return abspath(os.getcwd())

class HttpError(Exception):
    def __init__(self, code, msg, data, info):
        self.code, self.msg, self.data, self.info = \
            code, msg, data, info

class ImpURLopener(urllib.URLopener):
    version = 'impserve/' + __version__

    def http_error_default(self, url, fp, errcode, errmsg, headers):
        raise HttpError(errcode, errmsg, fp.read(), headers)

urllib._urlopener = ImpURLopener()

############################################################ plugin framework

class Plugin(type):
    def __init__(cls, name, bases, attrs):
        if not hasattr(cls, 'plugins'):
            # This branch only executes when processing the mount point itself.
            # So, since this is a new plugin type, not an implementation, this
            # class shouldn't be registered as a plugin. Instead, it sets up a
            # list where plugins can be registered later.
            cls.plugins = []
        else:
            # This must be a plugin implementation, which should be registered.
            # Simply appending it to the list is all that's needed to keep
            # track of it later.
            cls.plugins.append(cls)
            print 'Loaded plugin:', cls.__name__

    @staticmethod
    def load_from(dir):
        loaded, failed = [], []
        if not os.path.isdir(dir) or dir in sys.path:
            return [], []
        sys.path.append(dir)
        files = [f for f in os.listdir(dir) if f.lower().endswith('.py')]
        files.sort()
        for f in files:
            try:
                imp.load_source(os.path.splitext(f)[0], os.path.join(dir, f))
                loaded.append(f)
            except Exception, e:
                print "Unexpected error while loading %s: %s" % (f, e)
                failed.append(f)
        return loaded, failed

############################################################ extension points

class ProxyClient:
    """
    Plugins should implement a get_url() method returning the modified URL eg.

    def get_url(self, url):
        return url
    """
    __metaclass__ = Plugin

class ProxyResponse:
    """
    Plugins should implement a get_response() method, which accepts and returns
    the headers and the content.

    def get_response(self, url, headers, content):
        return headers, content
    """
    __metaclass__ = Plugin

###################################################################### server

class ImpProxyHandler(BaseHTTPServer.BaseHTTPRequestHandler):
    """ the actual HTTP handler class """

    server_version = 'impserve/' + __version__
    root_dir       = get_root()
    shelf_dirs     = [ join(root_dir, 'shelf') ]
    book_cache     = {}
    url_cache      = []

    ############################################################ HTTP handler

    def do_GET(self):
        """Serve a GET request."""

        (proto, host, path, param, qry, frag) = urlparse.urlparse(self.path)

        if proto != 'http' or not host:
            self.send_error(400, 'bad url %s' % self.path)
            return

        if host.lower().endswith(LOCAL_DOMAIN):
            self.handle_local_request(host, path, qry)
            return

        self.handle_proxy_request()

    do_POST = do_GET

    ############################################################## HTTP proxy

    def handle_proxy_request(self):
        url = self.path
        for plugin in ProxyClient.plugins:
            url = plugin().get_url(url)

        try:
            data = None
            if 'Content-Length' in self.headers:
                data = self.rfile.read(int(self.headers['Content-Length']))
            else:
                self.connection.setblocking(0)
                try: data = self.rfile.read()
                except: pass
                self.connection.setblocking(1)

            if 'Cookie' in self.headers:
                opener = ImpURLopener()
                opener.addheader('Cookie', self.headers['Content-Length'])
                f = opener.open(url, data=data)
            else:
                f = urllib.urlopen(url, data=data)
            code, msg, data, info = 200, 'OK', f.read(), f.info()
        except HttpError, e:
            code, msg, data, info = e.code, e.msg, e.data, e.info
        except:
            self.send_error(504, "Gateway Timeout")
            return
        self.send_response(code, msg)
        if 'Content-Disposition' in info:
            m = CONTENT_FILE.search(info['Content-Disposition'])
            if m:
                info['Content-Type'] = self.guess_type(m.group(1))
        for plugin in ProxyResponse.plugins:
            info, data = plugin().get_response(url, info, data)
        for name in info:
            self.send_header(name, info.getheader(name))
        self.end_headers()
        self.wfile.write(data)
        if code == 200 and 'text/html' in info['Content-Type']:
            self.url_cache.append(url)
            while len(self.url_cache) > URL_CACHE_MAX:
                del self.url_cache[0]

    ########################################################### local content

    def handle_local_request(self, host, path, qry):
        if self.path.startswith(BOOKLIST_PREFIX):
            while self.url_cache:
                del self.url_cache[0]
            params = cgi.parse_qs(qry)
            index, length = 0, 100
            if 'INDEX' in params:
                index = int(params['INDEX'][0])-1
            if 'REQUEST' in params:
                length = int(params['REQUEST'][0])
            self.reload_cache()
            data = self.get_booklist(index, length)
            self.send_response(200)
            self.send_header("Content-Length", len(data))
            self.send_header("Content-Type", 'text/x-booklist')
            self.end_headers()
            self.wfile.write(data)
        elif self.path.startswith(BOOK_PREFIX):
            book_id = self.path[len(BOOK_PREFIX):]
            delete = False
            if book_id.endswith('&DELETE=YES'):
                book_id, delete = book_id[:-len('&DELETE=YES')], True
            self.reload_cache()
            for info in self.book_cache.values():
                if info[3] == book_id:
                    if delete:
                        os.remove(info[0])
                        self.send_response(302, 'Found')
                        self.send_header("Location", BOOKLIST_PREFIX+'REQUEST=100')
                        self.end_headers()
                        return
                    data = open(info[0], 'rb')
                    self.send_response(200)
                    self.send_header("Content-Length", info[1])
                    self.send_header("Content-Type", 'application/x-softbook')
                    self.end_headers()
                    shutil.copyfileobj(data, self.wfile)
                    data.close()
            else:
                self.send_error(404, "File not found")
                return
        elif self.path.startswith(CONTENT_PREFIX):
            loc = join(self.root_dir, normpath(path)[1:])
            if isdir(loc):
                for index in INDEX_FILES:
                    if isfile(join(loc, index)):
                        loc = join(loc, index)
                        break
                else:
                    self.send_error(404, "File not found")
                    return
            elif not isfile(loc):
                    self.send_error(404, "File not found")
                    return
            self.send_response(200)
            self.send_header("Content-Type", self.guess_type(loc))
            self.send_header("Content-Length", getsize(loc))
            self.end_headers()
            shutil.copyfileobj(open(loc, 'rb'), self.wfile)
        elif self.path.startswith(REDIRECT_PREFIX):
            self.send_response(302, 'Found')
            self.send_header("Location", cgi.parse_qs(qry)['target'][0])
            self.end_headers()
        else:
            location = CONTENT_PREFIX
            if len(self.url_cache) >= 2:
                self.url_cache.pop()
                location = self.url_cache.pop()
            self.send_response(302, 'Found')
            self.send_header("Location", location)
            self.end_headers()

    ######################################################## booklist helpers

    def get_booklist(self, start=0, length=9999):
        """ return the updated book list """

        names = self.book_cache.keys()
        names.sort()

        result = "1\r\n"
        for book in names[start:start+length]:
            i = self.book_cache[book]
            result += "None:%s\t%s\t%s\t%s\t%d\t%s\t1\t17\r\n" % \
                      (i[3], i[5], i[6], i[4], i[1], BOOK_PREFIX+i[3])
        result += "\r\n\r\n"
        return result

    def guess_type(self, fname):
        return mimetypes.guess_type(fname)[0] or 'application/octet-stream'

    def reload_cache(self):
        for dir in self.shelf_dirs:
            self.book_cache = get_ebook_list(dir, self.book_cache)

    def address_string(self):
        return self.client_address[0]

######################################################################## main

def run(host, port, dirs=[]):
    mime_file = join(ImpProxyHandler.root_dir, 'mime.types')
    if not mimetypes.inited and isfile(mime_file):
        mimetypes.init(mime_file)
        print "Loading MIME definitions from", mime_file
    mimetypes.add_type('application/x-softbook', '.imp')
    Plugin.load_from(join(ImpProxyHandler.root_dir, 'plugins'))

    for dir in dirs:
        if isdir(dir):
            ImpProxyHandler.shelf_dirs.append(abspath(dir))

    httpd = BaseHTTPServer.HTTPServer((host,port), ImpProxyHandler)

    sname = httpd.socket.getsockname()
    print "impserve %s: starting server on %s:%s" % \
            (__version__, sname[0], sname[1])

    httpd.serve_forever()

def usage():
    print """
Usage: impserve [-OPTIONS] SHELF-DIRECTORIES
-h          show this help message.
-v          show the version.
-a ADDRESS  listen on the specified IP address (default: 0.0.0.0)
-p PORT     listen on the specified port       (default: 9090)
"""

def main():
    host, port = '', 9090
    try:
        opts, args = getopt.getopt(sys.argv[1:], "hva:p:")
    except getopt.GetoptError, err:
        print str(err)
        usage()
        sys.exit(2)
    for o, a in opts:
        if o == '-h':
            usage()
            sys.exit(0)
        elif o == '-v':
            print 'impserve %s' % __version__
            sys.exit(0)
        elif o == '-a':
            host = a
        elif o == '-p':
            port = int(a)
        else:
            print 'Unhandled option'
            sys.exit(3)

    run(host, port, args)
    sys.exit(0)