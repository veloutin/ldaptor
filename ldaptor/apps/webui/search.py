from twisted.internet import defer, protocol
from twisted.python import components
from ldaptor.protocols.ldap import ldapclient, ldapsyntax
from ldaptor.protocols.ldap import distinguishedname, ldapconnector
from ldaptor.protocols import pureber, pureldap
from ldaptor import ldapfilter, interfaces
from twisted.internet import reactor
from ldaptor.apps.webui import config
from ldaptor.apps.webui.uriquote import uriQuote
from ldaptor import weave

import os
from nevow import rend, inevow, loaders, url, tags, compy
from formless import annotate, webform, iformless, configurable

class IMove(components.Interface):
    """Entries being moved in the tree."""
    pass

class IMoveItem(annotate.TypedInterface):
    def move(self,
             context=annotate.Context()):
        pass
    move = annotate.autocallable(move)

    def cancel(self,
               context=annotate.Context()):
        pass
    cancel = annotate.autocallable(cancel)

class MoveItem(object):
    __implements__ = IMoveItem

    def __init__(self, entry):
        super(MoveItem, self).__init__()
        self.entry = entry

    def _remove(self, context):
        session = context.locate(inevow.ISession)
        move = session.getComponent(IMove)
        if move is None:
            return
        try:
            move.remove(self.entry)
        except ValueError:
            pass

    def move(self, context):
        cfg = context.locate(interfaces.ILDAPConfig)
        newDN = distinguishedname.DistinguishedName(
            self.entry.dn.split()[:1]
            + cfg.getBaseDN().split())
        d = self.entry.move(newDN)
        d.addCallback(lambda _: 'Moved %s to %s.' % (self.entry.dn, newDN))
        def _cb(r, context):
            self._remove(context)
            return r
        d.addCallback(_cb, context)
        return d

    def cancel(self, context):
        self._remove(context)
        return 'Cancelled move of %s' % self.entry.dn

def strScope(scope):
    if scope is pureldap.LDAP_SCOPE_wholeSubtree:
        return 'whole subtree'
    elif scope is pureldap.LDAP_SCOPE_singleLevel:
        return 'single level'
    elif scope is pureldap.LDAP_SCOPE_baseObject:
        return 'baseobject'
    else:
        raise RuntimeError, 'scope is not known: %r' % scope

class SearchForm(configurable.Configurable):
    __implements__ = configurable.Configurable.__implements__, inevow.IContainer

    filter = None
    scope = None

    def __init__(self):
        super(SearchForm, self).__init__(None)
        self.data = {}

    def getBindingNames(self, ctx):
        return ['search']

    def bind_search(self, ctx):
        l = []
        for field in config.getSearchFieldNames():
            l.append(annotate.Argument('search_%s' % field,
                                       annotate.String(label=field)))
        l.append(annotate.Argument('searchfilter',
                                   annotate.String(label="Advanced")))
        l.append(annotate.Argument(
            'scope',
            annotate.Choice(label="Search depth",
                            choices=[ pureldap.LDAP_SCOPE_wholeSubtree,
                                      pureldap.LDAP_SCOPE_singleLevel,
                                      pureldap.LDAP_SCOPE_baseObject,
                                      ],
                            stringify=strScope,
                            default=pureldap.LDAP_SCOPE_wholeSubtree)))

        return annotate.MethodBinding(
            'search',
            annotate.Method(arguments=l))

    def search(self, scope, searchfilter, **kw):
	filt=[]
	for k,v in kw.items():
            assert k.startswith('search_')
            if not k.startswith("search_"):
                continue
            k=k[len("search_"):]
            if v is None:
                continue
            v=v.strip()
            if v=='':
                continue

            # TODO escape ) in v
            # TODO handle unknown filter name right (old form open in browser etc)
            filter_ = config.getSearchFieldByName(k, vars={'input': v})
            filt.append(ldapfilter.parseFilter(filter_))
        if searchfilter:
            filt.append(ldapfilter.parseFilter(searchfilter))

	if filt:
	    if len(filt)==1:
		query=filt[0]
	    else:
		query=pureldap.LDAPFilter_and(filt)
	else:
	    query=pureldap.LDAPFilterMatchAll

        self.data.update(kw)
        self.data['scope'] = scope
        self.data['searchfilter'] = searchfilter
        self.filter = query
        return self

    def child(self, context, name):
        fn = getattr(self, 'child_%s' % name, None)
        if fn is None:
            return None
        else:
            return fn(context)

    def child_filter(self, context):
        return self.filter.asText()

    def child_results(self, context):
        assert self.filter is not None
        cfg = context.locate(interfaces.ILDAPConfig)

        c=ldapconnector.LDAPClientCreator(reactor, ldapclient.LDAPClient)
        d=c.connectAnonymously(cfg.getBaseDN(),
                               cfg.getServiceLocationOverrides())

        def _search(proto, base, searchFilter, scope):
            baseEntry = ldapsyntax.LDAPEntry(client=proto, dn=base)
            d=baseEntry.search(filterObject=searchFilter,
                               scope=scope,
                               sizeLimit=20,
                               sizeLimitIsNonFatal=True)
            return d
        d.addCallback(_search, cfg.getBaseDN(), self.filter, self.data['scope'])
        return d

    def child_base(self, context):
        cfg = context.locate(interfaces.ILDAPConfig)

        c=ldapconnector.LDAPClientCreator(reactor, ldapclient.LDAPClient)
        d=c.connectAnonymously(cfg.getBaseDN(),
                               cfg.getServiceLocationOverrides())

        def _search(proto, base):
            baseEntry = ldapsyntax.LDAPEntry(client=proto,
                                             dn=base)
            d=baseEntry.search(scope=pureldap.LDAP_SCOPE_baseObject,
                               sizeLimit=1)
            return d
        d.addCallback(_search, cfg.getBaseDN())

        def _first(results, dn):
            assert len(results)==1, \
                   "Expected one result, not %r" % results
            return {'dn': dn,
                    'attributes': results[0],
                    }
        d.addCallback(_first, cfg.getBaseDN())

        return d

    def __nonzero__(self):
        return self.filter is not None

def _upLink(request, name):
    if request.postpath:
        return (len(request.postpath)*"../") + "../" + name
    else:
        return "../" + name

def prettyLinkedDN(dn, baseObject, request):
    r=[]
    while (dn!=baseObject
           and dn!=distinguishedname.DistinguishedName(stringValue='')):
        firstPart=dn.split()[0]

        me=request.path.split('/', 4)[3]
        r.append(tags.a(href="../../%s" \
                        % _upLink(request,
                                  '/'.join([uriQuote(str(dn)), me]
                                           + request.postpath)))[
            str(firstPart)])
        r.append(',')
        dn=dn.up()

    r.append('%s\n' % str(dn))
    return r

class SearchPage(rend.Page):
    addSlash = True

    docFactory = loaders.xmlfile(
        'search.xhtml',
        templateDir=os.path.split(os.path.abspath(__file__))[0])

    def __init__(self):
        super(SearchPage, self).__init__()

    def data_css(self, context, data):
        request = context.locate(inevow.IRequest)
        root = url.URL.fromRequest(request).clear().parent().parent()
        return [
            root.child('form.css'),
            root.child('ldaptor.css'),
            ]

    def render_css_item(self, context, data):
        context.fillSlots('url', data)
        return context.tag

    def render_form(self, context, data):
        formDefaults = context.locate(iformless.IFormDefaults)
        methodDefaults = formDefaults.getAllDefaults('search')
        conf = self.locateConfigurable(context, '')
        for k,v in conf.data.items():
            methodDefaults[k] = v
        return webform.renderForms()

    def render_keyvalue(self, context, data):
        return weave.keyvalue(context, data)

    def render_keyvalue_item(self, context, data):
        return weave.keyvalue_item(context, data)

    def render_passthrough(self, context, data):
        return context.tag.clear()[data]

    def data_status(self, context, data):
        try:
            obj = context.locate(inevow.IStatusMessage)
        except KeyError:
            return ''

        if isinstance(obj, SearchForm):
            return ''
        else:
            return obj

    def render_if(self, context, data):
        r=context.tag.allPatterns(str(bool(data)))
        return context.tag.clear()[r]

    def configurable_(self, context):
        try:
            hand = context.locate(inevow.IHand)
        except KeyError:
            pass
        else:
            if isinstance(hand, SearchForm):
                return hand
        return SearchForm()

    def data_search(self, context, data):
        configurable = self.locateConfigurable(context, '')
        return configurable

    def data_header(self, context, data):
        request = context.locate(inevow.IRequest)
        u=url.URL.fromRequest(request)
        u=u.parent()
        l=[]
	l.append(tags.a(href=u.sibling("add"))["add new entry"])
	return l

    def data_navilink(self, context, data):
        cfg = context.locate(interfaces.ILDAPConfig)
        dn = cfg.getBaseDN()

	r=[]
	while dn!=distinguishedname.DistinguishedName(stringValue=''):
	    firstPart=dn.split()[0]
	    r.append(('../../%s' % uriQuote(str(dn)),
                      str(firstPart)))
	    dn=dn.up()
        return r

    def render_link(self, context, (url, desc)):
        context.fillSlots('url', url)
        context.fillSlots('description', desc)
        return context.tag

    def render_linkedDN(self, context, data):
        cfg = context.locate(interfaces.ILDAPConfig)
        request = context.locate(inevow.IRequest)
        return context.tag.clear()[prettyLinkedDN(data, cfg.getBaseDN(), request)]

    def render_entryLinks(self, context, data):
        request = context.locate(inevow.IRequest)
        u = url.URL.fromRequest(request)
        l = [ (u.parent().sibling('edit').child(uriQuote(data)), 'edit'),
              (u.parent().sibling('move').child(uriQuote(data)), 'move'),
              (u.parent().sibling('delete').child(uriQuote(data)), 'delete'),
              (u.parent().sibling('change_password').child(uriQuote(data)), 'change password'),
              ]
        return self.render_sequence(context, l)

    def render_listLen(self, context, data):
        if data is None:
            length = 0
        else:
            length = len(data)
            return context.tag.clear()[length]

    def render_mass_change_password(self, context, data):
        request = context.locate(inevow.IRequest)
        u = url.URL.fromRequest(request)
        u = u.parent().sibling("mass_change_password")
        u = u.child(uriQuote(data))
        return context.tag(href=u)

    def data_move(self, context, data):
        session = context.locate(inevow.ISession)
        if not session.getLoggedInRoot().loggedIn:
            return []
        move = session.getComponent(IMove)
        if move is None:
            return []
        return move

    def locateConfigurable(self, context, name):
        try:
            return super(SearchPage, self).locateConfigurable(context, name)
        except AttributeError:
            if name.startswith('move_'):
                pass
            else:
                raise

        dn = name[len('move_'):]

        session = context.locate(inevow.ISession)
        move = session.getComponent(IMove)
        if move is None:
            raise KeyError, name

        for entry in move:
            if entry.dn == dn:
                return iformless.IConfigurable(MoveItem(entry))

        raise KeyError, name

    def render_move(self, context, data):
        return webform.renderForms('move_%s' % data.dn)[context.tag]
        
def getSearchPage():
    r = SearchPage()
    return r