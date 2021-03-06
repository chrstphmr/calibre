#!/usr/bin/env python2
# vim:fileencoding=UTF-8:ts=4:sw=4:sta:et:sts=4:fdm=marker:ai
from __future__ import (unicode_literals, division, absolute_import,
                        print_function)

__license__   = 'GPL v3'
__copyright__ = '2013, Kovid Goyal <kovid at kovidgoyal.net>'
__docformat__ = 'restructuredtext en'

import json, sys, os, logging
from urllib import unquote
from collections import defaultdict

import regex
from cssutils import CSSParser
from PyQt5.Qt import (pyqtProperty, QEventLoop, Qt, QSize, QTimer,
                      pyqtSlot)
from PyQt5.QtWebKitWidgets import QWebPage, QWebView

from calibre.constants import iswindows
from calibre.ebooks.oeb.display.webview import load_html
from calibre.gui2 import must_use_qt

def normalize_font_properties(font):
    w = font.get('font-weight', None)
    if not w and w != 0:
        w = 'normal'
    w = unicode(w)
    w = {'normal':'400', 'bold':'700'}.get(w, w)
    if w not in {'100', '200', '300', '400', '500', '600', '700',
            '800', '900'}:
        w = '400'
    font['font-weight'] = w

    val = font.get('font-style', None)
    if val not in {'normal', 'italic', 'oblique'}:
        val = 'normal'
    font['font-style'] = val

    val = font.get('font-stretch', None)
    if val not in {'normal', 'ultra-condensed', 'extra-condensed', 'condensed',
                   'semi-condensed', 'semi-expanded', 'expanded',
                   'extra-expanded', 'ultra-expanded'}:
        val = 'normal'
    font['font-stretch'] = val
    return font

widths = {x:i for i, x in enumerate(('ultra-condensed',
        'extra-condensed', 'condensed', 'semi-condensed', 'normal',
        'semi-expanded', 'expanded', 'extra-expanded', 'ultra-expanded'
        ))}

def get_matching_rules(rules, font):
    matches = []

    # Filter on family
    for rule in reversed(rules):
        ff = frozenset(icu_lower(x) for x in font.get('font-family', []))
        if ff.intersection(rule['font-family']):
            matches.append(rule)
    if not matches:
        return []

    # Filter on font stretch
    width = widths[font.get('font-stretch', 'normal')]

    min_dist = min(abs(width-y['width']) for y in matches)
    nearest = [x for x in matches if abs(width-x['width']) == min_dist]
    if width <= 4:
        lmatches = [f for f in nearest if f['width'] <= width]
    else:
        lmatches = [f for f in nearest if f['width'] >= width]
    matches = (lmatches or nearest)

    # Filter on font-style
    fs = font.get('font-style', 'normal')
    order = {
            'oblique':['oblique', 'italic', 'normal'],
            'normal':['normal', 'oblique', 'italic']
        }.get(fs, ['italic', 'oblique', 'normal'])
    for q in order:
        m = [f for f in matches if f.get('font-style', 'normal') == q]
        if m:
            matches = m
            break

    # Filter on font weight
    fw = int(font.get('font-weight', '400'))
    if fw == 400:
        q = [400, 500, 300, 200, 100, 600, 700, 800, 900]
    elif fw == 500:
        q = [500, 400, 300, 200, 100, 600, 700, 800, 900]
    elif fw < 400:
        q = [fw] + list(xrange(fw-100, -100, -100)) + list(xrange(fw+100,
            100, 1000))
    else:
        q = [fw] + list(xrange(fw+100, 100, 1000)) + list(xrange(fw-100,
            -100, -100))
    for wt in q:
        m = [f for f in matches if f['weight'] == wt]
        if m:
            return m
    return []

def parse_font_families(parser, raw):
    style = parser.parseStyle('font-family:' + raw, validate=False).getProperty('font-family')
    for x in style.propertyValue:
        x = x.value
        if x:
            yield x

def get_pseudo_element_font_usage(pseudo_element_font_usage, first_letter_pat, parser):
    ans = []
    for font_dict, text, pseudo in pseudo_element_font_usage:
        text = text.strip()
        if pseudo == 'first-letter':
            prefix = first_letter_pat.match(text)
            if prefix is not None:
                text = prefix + text[len(prefix):].lstrip()[:1]
            else:
                text = text[:1]
        if text:
            font = font_dict.copy()
            font['text'] = text
            font['font-family'] = list(parse_font_families(parser, font['font-family']))
            ans.append(font)

    return ans

class Page(QWebPage):  # {{{

    def __init__(self, log):
        self.log = log
        QWebPage.__init__(self)
        self.js = None
        self.evaljs = self.mainFrame().evaluateJavaScript
        self.bridge_value = None
        nam = self.networkAccessManager()
        nam.setNetworkAccessible(nam.NotAccessible)
        self.longjs_counter = 0

    def javaScriptConsoleMessage(self, msg, lineno, msgid):
        self.log(u'JS:', unicode(msg))

    def javaScriptAlert(self, frame, msg):
        self.log(unicode(msg))

    @pyqtSlot(result=bool)
    def shouldInterruptJavaScript(self):
        if self.longjs_counter < 5:
            self.log('Long running javascript, letting it proceed')
            self.longjs_counter += 1
            return False
        self.log.warn('Long running javascript, aborting it')
        return True

    def _pass_json_value_getter(self):
        val = json.dumps(self.bridge_value)
        return val

    def _pass_json_value_setter(self, value):
        # Qt WebKit in Qt 4.x adds extra null bytes to the end of the string
        # if the JSON contains non-BMP characters
        self.bridge_value = json.loads(unicode(value).rstrip('\0'))

    _pass_json_value = pyqtProperty(str, fget=_pass_json_value_getter,
            fset=_pass_json_value_setter)

    def load_js(self):
        self.longjs_counter = 0
        if self.js is None:
            from calibre.utils.resources import compiled_coffeescript
            self.js = compiled_coffeescript('ebooks.oeb.display.utils')
            self.js += compiled_coffeescript('ebooks.oeb.polish.font_stats')
        self.mainFrame().addToJavaScriptWindowObject("py_bridge", self)
        self.evaljs(self.js)
        self.evaljs('''
        Object.defineProperty(py_bridge, 'value', {
               get : function() { return JSON.parse(this._pass_json_value); },
               set : function(val) { this._pass_json_value = JSON.stringify(val); }
        });
        ''')
# }}}

class StatsCollector(object):

    def __init__(self, container, do_embed=False):
        self.container = container
        self.log = self.logger = container.log
        self.do_embed = do_embed
        must_use_qt()
        self.parser = CSSParser(loglevel=logging.CRITICAL, log=logging.getLogger('calibre.css'))
        self.first_letter_pat = regex.compile(r'^[\p{Ps}\p{Ps}\p{Pe}\p{Pi}\p{Pf}\p{Po}]+', regex.VERSION1 | regex.UNICODE)
        self.capitalize_pat = regex.compile(r'[\p{L}\p{N}]', regex.VERSION1 | regex.UNICODE)

        self.loop = QEventLoop()
        self.view = QWebView()
        self.page = Page(self.log)
        self.view.setPage(self.page)
        self.page.setViewportSize(QSize(1200, 1600))

        self.view.loadFinished.connect(self.collect,
                type=Qt.QueuedConnection)

        self.render_queue = list(container.spine_items)
        self.font_stats = {}
        self.font_usage_map = {}
        self.font_spec_map = {}
        self.font_rule_map = {}
        self.all_font_rules = {}

        QTimer.singleShot(0, self.render_book)

        if self.loop.exec_() == 1:
            raise Exception('Failed to gather statistics from book, see log for details')

    def log_exception(self, *args):
        orig = self.log.filter_level
        try:
            self.log.filter_level = self.log.DEBUG
            self.log.exception(*args)
        finally:
            self.log.filter_level = orig

    def render_book(self):
        try:
            if not self.render_queue:
                self.loop.exit()
            else:
                self.render_next()
        except:
            self.log_exception('Rendering failed')
            self.loop.exit(1)

    def render_next(self):
        item = unicode(self.render_queue.pop(0))
        self.current_item = item
        load_html(item, self.view)

    def collect(self, ok):
        if not ok:
            self.log.error('Failed to render document: %s'%self.container.relpath(self.current_item))
            self.loop.exit(1)
            return
        try:
            self.page.load_js()
            self.collect_font_stats()
        except:
            self.log_exception('Failed to collect font stats from: %s'%self.container.relpath(self.current_item))
            self.loop.exit(1)
            return

        self.render_book()

    def href_to_name(self, href, warn_name):
        if not href.startswith('file://'):
            self.log.warn('Non-local URI in', warn_name, ':', href, 'ignoring')
            return None
        src = href[len('file://'):]
        if iswindows and len(src) > 2 and (src[0], src[2]) == ('/', ':'):
            src = src[1:]
        src = src.replace('/', os.sep)
        src = unquote(src)
        name = self.container.abspath_to_name(src)
        if not self.container.has_name(name):
            self.log.warn('Missing resource', href, 'in', warn_name,
                          'ignoring')
            return None
        return name

    def collect_font_stats(self):
        self.page.evaljs('window.font_stats.get_font_face_rules()')
        font_face_rules = self.page.bridge_value
        if not isinstance(font_face_rules, list):
            raise Exception('Unknown error occurred while reading font-face rules')

        # Weed out invalid font-face rules
        rules = []
        import tinycss
        parser = tinycss.make_full_parser()
        for rule in font_face_rules:
            ff = rule.get('font-family', None)
            if not ff:
                continue
            style = self.parser.parseStyle('font-family:%s'%ff, validate=False)
            ff = [x.value for x in
                  style.getProperty('font-family').propertyValue]
            if not ff or ff[0] == 'inherit':
                continue
            rule['font-family'] = frozenset(icu_lower(f) for f in ff)
            src = rule.get('src', None)
            if not src:
                continue
            try:
                tokens = parser.parse_stylesheet('@font-face { src: %s }' % src).rules[0].declarations[0].value
            except Exception:
                self.log.warn('Failed to parse @font-family src: %s' % src)
                continue
            for token in tokens:
                if token.type == 'URI':
                    uv = token.value
                    if uv:
                        sn = self.href_to_name(uv, '@font-face rule')
                        if sn is not None:
                            rule['src'] = sn
                            break
            else:
                self.log.warn('The @font-face rule refers to a font file that does not exist in the book: %s' % src)
                continue
            normalize_font_properties(rule)
            rule['width'] = widths[rule['font-stretch']]
            rule['weight'] = int(rule['font-weight'])
            rules.append(rule)

        if not rules and not self.do_embed:
            return

        self.font_rule_map[self.container.abspath_to_name(self.current_item)] = rules
        for rule in rules:
            self.all_font_rules[rule['src']] = rule

        for rule in rules:
            if rule['src'] not in self.font_stats:
                self.font_stats[rule['src']] = set()

        self.page.evaljs('window.font_stats.get_font_usage()')
        font_usage = self.page.bridge_value
        if not isinstance(font_usage, list):
            raise Exception('Unknown error occurred while reading font usage')
        self.page.evaljs('window.font_stats.get_pseudo_element_font_usage()')
        pseudo_element_font_usage = self.page.bridge_value
        if not isinstance(pseudo_element_font_usage, list):
            raise Exception('Unknown error occurred while reading pseudo element font usage')
        font_usage += get_pseudo_element_font_usage(pseudo_element_font_usage, self.first_letter_pat, self.parser)
        exclude = {'\n', '\r', '\t'}
        self.font_usage_map[self.container.abspath_to_name(self.current_item)] = fu = defaultdict(dict)
        bad_fonts = {'serif', 'sans-serif', 'monospace', 'cursive', 'fantasy', 'sansserif', 'inherit'}
        for font in font_usage:
            text = set()
            for t in font['text']:
                tt = (font['text-transform'] or '').lower()
                if tt != 'none':
                    if tt == 'uppercase':
                        t = icu_upper(t)
                    elif tt == 'lowercase':
                        t = icu_lower(t)
                    elif tt == 'capitalize':
                        m = self.capitalize_pat.search(t)
                        if m is not None:
                            t += icu_upper(m.group())
                fv = (font['font-variant'] or '').lower()
                if fv in {'smallcaps', 'small-caps', 'all-small-caps', 'petite-caps', 'all-petite-caps', 'unicase'}:
                    t += icu_upper(t)  # for renderers that try to fake small-caps by using small normal caps
                text |= frozenset(t)
            text.difference_update(exclude)
            if not text:
                continue
            normalize_font_properties(font)
            for rule in get_matching_rules(rules, font):
                self.font_stats[rule['src']] |= text
            if self.do_embed:
                ff = [icu_lower(x) for x in font.get('font-family', [])]
                if ff and ff[0] not in bad_fonts:
                    keys = {'font-weight', 'font-style', 'font-stretch', 'font-family'}
                    key = frozenset(((k, ff[0] if k == 'font-family' else v) for k, v in font.iteritems() if k in keys))
                    val = fu[key]
                    if not val:
                        val.update({k:(font[k][0] if k == 'font-family' else font[k]) for k in keys})
                        val['text'] = set()
                    val['text'] |= text
        self.font_usage_map[self.container.abspath_to_name(self.current_item)] = dict(fu)

        if self.do_embed:
            self.page.evaljs('window.font_stats.get_font_families()')
            font_families = self.page.bridge_value
            if not isinstance(font_families, dict):
                raise Exception('Unknown error occurred while reading font families')
            self.font_spec_map[self.container.abspath_to_name(self.current_item)] = fs = set()
            for font_dict, text, pseudo in pseudo_element_font_usage:
                font_families[font_dict['font-family']] = True
            for raw in font_families.iterkeys():
                for x in parse_font_families(self.parser, raw):
                    if x.lower() not in bad_fonts:
                        fs.add(x)

if __name__ == '__main__':
    from calibre.ebooks.oeb.polish.container import get_container
    from calibre.utils.logging import default_log
    default_log.filter_level = default_log.DEBUG
    ebook = get_container(sys.argv[-1], default_log)
    from pprint import pprint
    pprint(StatsCollector(ebook, do_embed=True).font_stats)
