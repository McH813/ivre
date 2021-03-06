#! /usr/bin/env python

# This file is part of IVRE.
# Copyright 2011 - 2020 Pierre LALET <pierre@droids-corp.org>
#
# IVRE is free software: you can redistribute it and/or modify it
# under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# IVRE is distributed in the hope that it will be useful, but WITHOUT
# ANY WARRANTY; without even the implied warranty of MERCHANTABILITY
# or FITNESS FOR A PARTICULAR PURPOSE.  See the GNU General Public
# License for more details.
#
# You should have received a copy of the GNU General Public License
# along with IVRE. If not, see <http://www.gnu.org/licenses/>.


"""This sub-module contains functions to handle JSON output from zgrab.

"""


from future.utils import viewitems
import re


from ivre import utils
from ivre.xmlnmap import add_cert_hostnames, create_ssl_cert, create_http_ls


_EXPR_TITLE = re.compile('<title[^>]*>([^<]*)</title>', re.I)
_EXPR_OWA_VERSION = re.compile('"/owa/(?:auth/)?((?:[0-9]+\\.)+[0-9]+)/')


def zgrap_parser_http(data, hostrec):
    """This function handles data from `{"data": {"http": [...]}}`
records. `data` should be the content, i.e. the `[...]`. It should
consist of simple dictionary, that may contain a `"response"` key
and/or a `"redirect_response_chain"` key.

The output is a port dict (i.e., the content of the "ports" key of an
`nmap` of `view` record in IVRE), that may be empty.

    """
    if not data:
        return {}
    # for zgrab2 results
    if 'result' in data:
        data.update(data.pop('result'))
    if 'response' not in data:
        utils.LOGGER.warning('Missing "response" field in zgrab HTTP result')
        return {}
    resp = data['response']
    needed_fields = set(["request", "status_code", "status_line"])
    missing_fields = needed_fields.difference(resp)
    if missing_fields:
        utils.LOGGER.warning(
            'Missing field%s %s in zgrab HTTP result',
            's' if len(missing_fields) > 1 else '',
            ', '.join(repr(fld) for fld in missing_fields),
        )
        return {}
    req = resp['request']
    url = req.get('url')
    res = {"service_name": "http", "service_method": "probed",
           "state_state": "open", "state_reason": "response",
           "protocol": "tcp"}
    tls = None
    try:
        tls = req['tls_handshake']
    except KeyError:
        # zgrab2
        try:
            tls = req['tls_log']['handshake_log']
        except KeyError:
            pass
    if tls is not None:
        res['service_tunnel'] = 'ssl'
        try:
            cert = tls['server_certificates']['certificate']['raw']
        except KeyError:
            pass
        else:
            output, info = create_ssl_cert(cert.encode(), b64encoded=True)
            if info:
                res.setdefault('scripts', []).append({
                    'id': 'ssl-cert',
                    'output': output,
                    'ssl-cert': info,
                })
                for cert in info:
                    add_cert_hostnames(cert,
                                       hostrec.setdefault('hostnames', []))
    if url:
        port = None
        if ':' in url.get('host', ''):
            try:
                port = int(url['host'].split(':', 1)[1])
            except ValueError:
                pass
        if port is None:
            if url.get('scheme') == 'https':
                port = 443
            else:
                port = 80
        # Specific paths
        if url.get('path').endswith('/.git/index'):
            if resp.get('status_code') != 200:
                return {}
            if not resp.get('body', '').startswith('DIRC'):
                return {}
            # Due to an issue with ZGrab2 output, we cannot, for now,
            # process the content of the file. See
            # <https://github.com/zmap/zgrab2/issues/263>.
            repository = '%s:%d%s' % (hostrec['addr'], port, url['path'][:-5])
            res['port'] = port
            res.setdefault('scripts', []).append({
                'id': 'http-git',
                'output': '\n  %s\n    Git repository found!\n' % repository,
                'http-git': [{'repository': repository,
                              'files-found': [".git/index"]}],
            })
            return res
        if url.get('path').endswith('/owa/auth/logon.aspx'):
            if resp.get('status_code') != 200:
                return {}
            version = set(
                m.group(1)
                for m in _EXPR_OWA_VERSION.finditer(resp.get('body', ''))
            )
            if not version:
                return {}
            version = sorted(version,
                             key=lambda v: [int(x) for x in v.split('.')])
            res['port'] = port
            path = url['path'][:-15]
            if len(version) > 1:
                output = (
                    'OWA: path %s, version %s (multiple versions found!)' % (
                        path,
                        ' / '.join(version),
                    )
                )
            else:
                output = 'OWA: path %s, version %s' % (path, version[0])
            res.setdefault('scripts', []).append({
                'id': 'http-app',
                'output': output,
                'http-app': [{'path': path,
                              'application': 'OWA',
                              'version': version[0]}],
            })
            return res
    elif req.get('tls_handshake') or req.get('tls_log'):
        # zgrab / zgrab2
        port = 443
    else:
        port = 80
    res['port'] = port
    # Since Zgrab does not preserve the order of the headers, we need
    # to reconstruct a banner to use Nmap fingerprints
    banner = (utils.nmap_decode_data(resp['protocol']['name']) + b' ' +
              utils.nmap_decode_data(resp['status_line']) + b"\r\n")
    if resp.get('headers'):
        headers = resp['headers']
        # the order will be incorrect!
        line = '%s %s' % (resp['protocol']['name'], resp['status_line'])
        http_hdrs = [{'name': '_status', 'value': line}]
        output = [line]
        if 'unknown' in headers:
            for unk in headers.pop('unknown'):
                headers[unk['key']] = unk['value']
        for hdr, values in viewitems(headers):
            hdr = hdr.replace('_', '-')
            for val in values:
                http_hdrs.append({'name': hdr, 'value': val})
                output.append('%s: %s' % (hdr, val))
        if http_hdrs:
            method = req.get('method')
            if method:
                output.append('')
                output.append('(Request type: %s)' % method)
            res.setdefault('scripts', []).append({
                'id': 'http-headers', 'output': '\n'.join(output),
                'http-headers': http_hdrs,
            })
        if headers.get('server'):
            server = resp['headers']['server']
            res.setdefault('scripts', []).append({
                'id': 'http-server-header', 'output': server[0],
                'http-server-header': server,
            })
            banner += (b"Server: " + utils.nmap_decode_data(server[0]) +
                       b"\r\n\r\n")
    info = utils.match_nmap_svc_fp(banner, proto="tcp", probe="GetRequest")
    if info:
        res.update(info)
    if resp.get('body'):
        body = resp['body']
        res.setdefault('scripts', []).append({
            'id': 'http-content',
            'output': utils.nmap_encode_data(body.encode()),
        })
        match = _EXPR_TITLE.search(body)
        if match is not None:
            title = match.groups()[0]
            res['scripts'].append({
                'id': 'http-title', 'output': title,
                'http-title': {'title': title},
            })
        script_http_ls = create_http_ls(body, url=url)
        if script_http_ls is not None:
            res.setdefault('scripts', []).append(script_http_ls)
    return res


ZGRAB_PARSERS = {'http': zgrap_parser_http}
