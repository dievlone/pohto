# hgweb/hgwebdir_mod.py - Web interface for a directory of repositories.
#
# Copyright 21 May 2005 - (c) 2005 Jake Edge <jake@edge2.net>
# Copyright 2005, 2006 Matt Mackall <mpm@selenic.com>
#
# This software may be used and distributed according to the terms of the
# GNU General Public License version 2 or any later version.

from __future__ import absolute_import

import gc
import os
import time

from ..i18n import _

from .common import (
    ErrorResponse,
    HTTP_SERVER_ERROR,
    cspvalues,
    get_contact,
    get_mtime,
    ismember,
    paritygen,
    staticfile,
    statusmessage,
)

from .. import (
    configitems,
    encoding,
    error,
    hg,
    profiling,
    pycompat,
    registrar,
    scmutil,
    templater,
    templateutil,
    ui as uimod,
    util,
)

from . import (
    hgweb_mod,
    request as requestmod,
    webutil,
    wsgicgi,
)
from ..utils import dateutil

def cleannames(items):
    return [(util.pconvert(name).strip('/'), path) for name, path in items]

def findrepos(paths):
    repos = []
    for prefix, root in cleannames(paths):
        roothead, roottail = os.path.split(root)
        # "foo = /bar/*" or "foo = /bar/**" lets every repo /bar/N in or below
        # /bar/ be served as as foo/N .
        # '*' will not search inside dirs with .hg (except .hg/patches),
        # '**' will search inside dirs with .hg (and thus also find subrepos).
        try:
            recurse = {'*': False, '**': True}[roottail]
        except KeyError:
            repos.append((prefix, root))
            continue
        roothead = os.path.normpath(os.path.abspath(roothead))
        paths = scmutil.walkrepos(roothead, followsym=True, recurse=recurse)
        repos.extend(urlrepos(prefix, roothead, paths))
    return repos

def urlrepos(prefix, roothead, paths):
    """yield url paths and filesystem paths from a list of repo paths

    >>> conv = lambda seq: [(v, util.pconvert(p)) for v,p in seq]
    >>> conv(urlrepos(b'hg', b'/opt', [b'/opt/r', b'/opt/r/r', b'/opt']))
    [('hg/r', '/opt/r'), ('hg/r/r', '/opt/r/r'), ('hg', '/opt')]
    >>> conv(urlrepos(b'', b'/opt', [b'/opt/r', b'/opt/r/r', b'/opt']))
    [('r', '/opt/r'), ('r/r', '/opt/r/r'), ('', '/opt')]
    """
    for path in paths:
        path = os.path.normpath(path)
        yield (prefix + '/' +
               util.pconvert(path[len(roothead):]).lstrip('/')).strip('/'), path

def readallowed(ui, req):
    """Check allow_read and deny_read config options of a repo's ui object
    to determine user permissions.  By default, with neither option set (or
    both empty), allow all users to read the repo.  There are two ways a
    user can be denied read access:  (1) deny_read is not empty, and the
    user is unauthenticated or deny_read contains user (or *), and (2)
    allow_read is not empty and the user is not in allow_read.  Return True
    if user is allowed to read the repo, else return False."""

    user = req.remoteuser

    deny_read = ui.configlist('web', 'deny_read', untrusted=True)
    if deny_read and (not user or ismember(ui, user, deny_read)):
        return False

    allow_read = ui.configlist('web', 'allow_read', untrusted=True)
    # by default, allow reading if no allow_read option has been set
    if not allow_read or ismember(ui, user, allow_read):
        return True

    return False

def rawindexentries(ui, repos, req, subdir=''):
    descend = ui.configbool('web', 'descend')
    collapse = ui.configbool('web', 'collapse')
    seenrepos = set()
    seendirs = set()
    for name, path in repos:

        if not name.startswith(subdir):
            continue
        name = name[len(subdir):]
        directory = False

        if '/' in name:
            if not descend:
                continue

            nameparts = name.split('/')
            rootname = nameparts[0]

            if not collapse:
                pass
            elif rootname in seendirs:
                continue
            elif rootname in seenrepos:
                pass
            else:
                directory = True
                name = rootname

                # redefine the path to refer to the directory
                discarded = '/'.join(nameparts[1:])

                # remove name parts plus accompanying slash
                path = path[:-len(discarded) - 1]

                try:
                    r = hg.repository(ui, path)
                    directory = False
                except (IOError, error.RepoError):
                    pass

        parts = [
            req.apppath.strip('/'),
            subdir.strip('/'),
            name.strip('/'),
        ]
        url = '/' + '/'.join(p for p in parts if p) + '/'

        # show either a directory entry or a repository
        if directory:
            # get the directory's time information
            try:
                d = (get_mtime(path), dateutil.makedate()[1])
            except OSError:
                continue

            # add '/' to the name to make it obvious that
            # the entry is a directory, not a regular repository
            row = {'contact': "",
                   'contact_sort': "",
                   'name': name + '/',
                   'name_sort': name,
                   'url': url,
                   'description': "",
                   'description_sort': "",
                   'lastchange': d,
                   'lastchange_sort': d[1] - d[0],
                   'archives': templateutil.mappinglist([]),
                   'isdirectory': True,
                   'labels': templateutil.hybridlist([], name='label'),
                   }

            seendirs.add(name)
            yield row
            continue

        u = ui.copy()
        try:
            u.readconfig(os.path.join(path, '.hg', 'hgrc'))
        except Exception as e:
            u.warn(_('error reading %s/.hg/hgrc: %s\n') % (path, e))
            continue

        def get(section, name, default=uimod._unset):
            return u.config(section, name, default, untrusted=True)

        if u.configbool("web", "hidden", untrusted=True):
            continue

        if not readallowed(u, req):
            continue

        # update time with local timezone
        try:
            r = hg.repository(ui, path)
        except IOError:
            u.warn(_('error accessing repository at %s\n') % path)
            continue
        except error.RepoError:
            u.warn(_('error accessing repository at %s\n') % path)
            continue
        try:
            d = (get_mtime(r.spath), dateutil.makedate()[1])
        except OSError:
            continue

        contact = get_contact(get)
        description = get("web", "description")
        seenrepos.add(name)
        name = get("web", "name", name)
        labels = u.configlist('web', 'labels', untrusted=True)
        row = {'contact': contact or "unknown",
               'contact_sort': contact.upper() or "unknown",
               'name': name,
               'name_sort': name,
               'url': url,
               'description': description or "unknown",
               'description_sort': description.upper() or "unknown",
               'lastchange': d,
               'lastchange_sort': d[1] - d[0],
               'archives': webutil.archivelist(u, "tip", url),
               'isdirectory': None,
               'labels': templateutil.hybridlist(labels, name='label'),
               }

        yield row

def _indexentriesgen(context, ui, repos, req, stripecount, sortcolumn,
                     descending, subdir):
    rows = rawindexentries(ui, repos, req, subdir=subdir)

    sortdefault = None, False

    if sortcolumn and sortdefault != (sortcolumn, descending):
        sortkey = '%s_sort' % sortcolumn
        rows = sorted(rows, key=lambda x: x[sortkey],
                      reverse=descending)

    for row, parity in zip(rows, paritygen(stripecount)):
        row['parity'] = parity
        yield row

def indexentries(ui, repos, req, stripecount, sortcolumn='',
                 descending=False, subdir=''):
    args = (ui, repos, req, stripecount, sortcolumn, descending, subdir)
    return templateutil.mappinggenerator(_indexentriesgen, args=args)

class hgwebdir(object):
    """HTTP server for multiple repositories.

    Given a configuration, different repositories will be served depending
    on the request path.

    Instances are typically used as WSGI applications.
    """
    def __init__(self, conf, baseui=None):
        self.conf = conf
        self.baseui = baseui
        self.ui = None
        self.lastrefresh = 0
        self.motd = None
        self.refresh()

    def refresh(self):
        if self.ui:
            refreshinterval = self.ui.configint('web', 'refreshinterval')
        else:
            item = configitems.coreitems['web']['refreshinterval']
            refreshinterval = item.default

        # refreshinterval <= 0 means to always refresh.
        if (refreshinterval > 0 and
            self.lastrefresh + refreshinterval > time.time()):
            return

        if self.baseui:
            u = self.baseui.copy()
        else:
            u = uimod.ui.load()
            u.setconfig('ui', 'report_untrusted', 'off', 'hgwebdir')
            u.setconfig('ui', 'nontty', 'true', 'hgwebdir')
            # displaying bundling progress bar while serving feels wrong and may
            # break some wsgi implementations.
            u.setconfig('progress', 'disable', 'true', 'hgweb')

        if not isinstance(self.conf, (dict, list, tuple)):
            map = {'paths': 'hgweb-paths'}
            if not os.path.exists(self.conf):
                raise error.Abort(_('config file %s not found!') % self.conf)
            u.readconfig(self.conf, remap=map, trust=True)
            paths = []
            for name, ignored in u.configitems('hgweb-paths'):
                for path in u.configlist('hgweb-paths', name):
                    paths.append((name, path))
        elif isinstance(self.conf, (list, tuple)):
            paths = self.conf
        elif isinstance(self.conf, dict):
            paths = self.conf.items()

        repos = findrepos(paths)
        for prefix, root in u.configitems('collections'):
            prefix = util.pconvert(prefix)
            for path in scmutil.walkrepos(root, followsym=True):
                repo = os.path.normpath(path)
                name = util.pconvert(repo)
                if name.startswith(prefix):
                    name = name[len(prefix):]
                repos.append((name.lstrip('/'), repo))

        self.repos = repos
        self.ui = u
        encoding.encoding = self.ui.config('web', 'encoding')
        self.style = self.ui.config('web', 'style')
        self.templatepath = self.ui.config('web', 'templates', untrusted=False)
        self.stripecount = self.ui.config('web', 'stripes')
        if self.stripecount:
            self.stripecount = int(self.stripecount)
        prefix = self.ui.config('web', 'prefix')
        if prefix.startswith('/'):
            prefix = prefix[1:]
        if prefix.endswith('/'):
            prefix = prefix[:-1]
        self.prefix = prefix
        self.lastrefresh = time.time()

    def run(self):
        if not encoding.environ.get('GATEWAY_INTERFACE',
                                    '').startswith("CGI/1."):
            raise RuntimeError("This function is only intended to be "
                               "called while running as a CGI script.")
        wsgicgi.launch(self)

    def __call__(self, env, respond):
        baseurl = self.ui.config('web', 'baseurl')
        req = requestmod.parserequestfromenv(env, altbaseurl=baseurl)
        res = requestmod.wsgiresponse(req, respond)

        return self.run_wsgi(req, res)

    def run_wsgi(self, req, res):
        profile = self.ui.configbool('profiling', 'enabled')
        with profiling.profile(self.ui, enabled=profile):
            try:
                for r in self._runwsgi(req, res):
                    yield r
            finally:
                # There are known cycles in localrepository that prevent
                # those objects (and tons of held references) from being
                # collected through normal refcounting. We mitigate those
                # leaks by performing an explicit GC on every request.
                # TODO remove this once leaks are fixed.
                # TODO only run this on requests that create localrepository
                # instances instead of every request.
                gc.collect()

    def _runwsgi(self, req, res):
        try:
            self.refresh()

            csp, nonce = cspvalues(self.ui)
            if csp:
                res.headers['Content-Security-Policy'] = csp

            virtual = req.dispatchpath.strip('/')
            tmpl = self.templater(req, nonce)
            ctype = tmpl.render('mimetype', {'encoding': encoding.encoding})

            # Global defaults. These can be overridden by any handler.
            res.status = '200 Script output follows'
            res.headers['Content-Type'] = ctype

            # a static file
            if virtual.startswith('static/') or 'static' in req.qsparams:
                if virtual.startswith('static/'):
                    fname = virtual[7:]
                else:
                    fname = req.qsparams['static']
                static = self.ui.config("web", "static", untrusted=False)
                if not static:
                    tp = self.templatepath or templater.templatepaths()
                    if isinstance(tp, str):
                        tp = [tp]
                    static = [os.path.join(p, 'static') for p in tp]

                staticfile(static, fname, res)
                return res.sendresponse()

            # top-level index

            repos = dict(self.repos)

            if (not virtual or virtual == 'index') and virtual not in repos:
                return self.makeindex(req, res, tmpl)

            # nested indexes and hgwebs

            if virtual.endswith('/index') and virtual not in repos:
                subdir = virtual[:-len('index')]
                if any(r.startswith(subdir) for r in repos):
                    return self.makeindex(req, res, tmpl, subdir)

            def _virtualdirs():
                # Check the full virtual path, each parent, and the root ('')
                if virtual != '':
                    yield virtual

                    for p in util.finddirs(virtual):
                        yield p

                yield ''

            for virtualrepo in _virtualdirs():
                real = repos.get(virtualrepo)
                if real:
                    # Re-parse the WSGI environment to take into account our
                    # repository path component.
                    uenv = req.rawenv
                    if pycompat.ispy3:
                        uenv = {k.decode('latin1'): v for k, v in
                                uenv.iteritems()}
                    req = requestmod.parserequestfromenv(
                        uenv, reponame=virtualrepo,
                        altbaseurl=self.ui.config('web', 'baseurl'),
                        # Reuse wrapped body file object otherwise state
                        # tracking can get confused.
                        bodyfh=req.bodyfh)
                    try:
                        # ensure caller gets private copy of ui
                        repo = hg.repository(self.ui.copy(), real)
                        return hgweb_mod.hgweb(repo).run_wsgi(req, res)
                    except IOError as inst:
                        msg = encoding.strtolocal(inst.strerror)
                        raise ErrorResponse(HTTP_SERVER_ERROR, msg)
                    except error.RepoError as inst:
                        raise ErrorResponse(HTTP_SERVER_ERROR, bytes(inst))

            # browse subdirectories
            subdir = virtual + '/'
            if [r for r in repos if r.startswith(subdir)]:
                return self.makeindex(req, res, tmpl, subdir)

            # prefixes not found
            res.status = '404 Not Found'
            res.setbodygen(tmpl.generate('notfound', {'repo': virtual}))
            return res.sendresponse()

        except ErrorResponse as e:
            res.status = statusmessage(e.code, pycompat.bytestr(e))
            res.setbodygen(tmpl.generate('error', {'error': e.message or ''}))
            return res.sendresponse()
        finally:
            tmpl = None

    def makeindex(self, req, res, tmpl, subdir=""):
        self.refresh()
        sortable = ["name", "description", "contact", "lastchange"]
        sortcolumn, descending = None, False
        if 'sort' in req.qsparams:
            sortcolumn = req.qsparams['sort']
            descending = sortcolumn.startswith('-')
            if descending:
                sortcolumn = sortcolumn[1:]
            if sortcolumn not in sortable:
                sortcolumn = ""

        sort = [("sort_%s" % column,
                 "%s%s" % ((not descending and column == sortcolumn)
                            and "-" or "", column))
                for column in sortable]

        self.refresh()

        entries = indexentries(self.ui, self.repos, req,
                               self.stripecount, sortcolumn=sortcolumn,
                               descending=descending, subdir=subdir)

        mapping = {
            'entries': entries,
            'subdir': subdir,
            'pathdef': hgweb_mod.makebreadcrumb('/' + subdir, self.prefix),
            'sortcolumn': sortcolumn,
            'descending': descending,
        }
        mapping.update(sort)
        res.setbodygen(tmpl.generate('index', mapping))
        return res.sendresponse()

    def templater(self, req, nonce):

        def config(section, name, default=uimod._unset, untrusted=True):
            return self.ui.config(section, name, default, untrusted)

        vars = {}
        styles, (style, mapfile) = hgweb_mod.getstyle(req, config,
                                                      self.templatepath)
        if style == styles[0]:
            vars['style'] = style

        sessionvars = webutil.sessionvars(vars, r'?')
        logourl = config('web', 'logourl')
        logoimg = config('web', 'logoimg')
        staticurl = (config('web', 'staticurl')
                     or req.apppath.rstrip('/') + '/static/')
        if not staticurl.endswith('/'):
            staticurl += '/'

        defaults = {
            "encoding": encoding.encoding,
            "url": req.apppath + '/',
            "logourl": logourl,
            "logoimg": logoimg,
            "staticurl": staticurl,
            "sessionvars": sessionvars,
            "style": style,
            "nonce": nonce,
        }
        templatekeyword = registrar.templatekeyword(defaults)
        @templatekeyword('motd', requires=())
        def motd(context, mapping):
            if self.motd is not None:
                yield self.motd
            else:
                yield config('web', 'motd')

        tmpl = templater.templater.frommapfile(mapfile, defaults=defaults)
        return tmpl