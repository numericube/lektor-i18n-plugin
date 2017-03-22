# -*- coding: utf-8 -*-
from lektor.pluginsystem import Plugin
from lektor.db import Page
from lektor.metaformat import tokenize
from lektor.reporter import reporter
from lektor.types.flow import FlowType, process_flowblock_data, discover_relevant_flowblock_models
from lektor.utils import portable_popen, locate_executable
from lektor.environment import PRIMARY_ALT
from lektor.filecontents import FileContents
from lektor.context import get_ctx

from pprint import PrettyPrinter
from os.path import relpath, join, exists, dirname
from os import walk, makedirs
from datetime import datetime
import time, gettext, urlparse
import fnmatch, re
import tempfile


_command_re = re.compile(r'([a-zA-Z0-9.-_]+):')
_block2_re = re.compile(r'^###(#+)\s*([^#]*?)\s*###(#+)\s*$') # derived from lektor.types.flow but allows more dash signs

def truncate(s, length=32):
    return (s[:length] + '..') if len(s) > length else s

POT_HEADER="""msgid ""
msgstr ""
"Project-Id-Version: PACKAGE VERSION\\n"
"Report-Msgid-Bugs-To: \\n"
"POT-Creation-Date: %(NOW)s\\n"
"PO-Revision-Date: YEAR-MO-DA HO:MI+ZONE\\n"
"Last-Translator: FULL NAME <EMAIL@ADDRESS>\\n"
"Language-Team: %(LANGUAGE)s <LL@li.org>\\n"
"Language: %(LANGUAGE)s\\n"
"MIME-Version: 1.0\\n"
"Content-Type: text/plain; charset=UTF-8\\n"
"Content-Transfer-Encoding: 8bit\\n"

"""

class Translations(object):
    """Memory of translations"""

    def __init__(self):
        self.translations={} # dict like {'text' : ['source1', 'source2',...],}

    def add(self, text, source):

        if not text in self.translations.keys():
            self.translations[text]=[]
            reporter.report_debug_info('added to translation memory : ', truncate(text))
        if not source in self.translations[text]:
            self.translations[text].append(source)
            # reporter.report_debug_info('adding source "%s" to "%s" translation memory'%(source, truncate(text)))

    def __repr__(self):
        return PrettyPrinter(2).pformat(self.translations)

    def as_pot(self, content_language):
        """returns a POT version of the translation dictionnary"""
        NOW=datetime.now().strftime('%Y-%m-%d %H:%M')
        NOW+='+%s'%(time.tzname[0])
        result=POT_HEADER % {  'LANGUAGE' : content_language, 'NOW' : NOW}

        for s, paths in self.translations.items():
            result+="#: %s\n"%" ".join(paths)
            result+='msgid "%s"\n'% s.replace('"','\\"')
            result+='msgstr ""\n\n'
        return result

    def write_pot(self, pot_filename, language):
        with open(pot_filename,'w') as f:
            f.write(self.as_pot(language).encode("utf-8"))

    def merge_pot(self, from_filenames, to_filename):
        msgcat=locate_executable('msgcat')
        cmdline=[msgcat, "--use-first"]
        cmdline.extend(from_filenames)
        cmdline.extend(("-o", to_filename))
        reporter.report_debug_info('msgcat cmd line', cmdline)
        portable_popen(cmdline).wait()

    def parse_templates(self, to_filename):
        pybabel=locate_executable('pybabel')
        cmdline=[pybabel, 'extract', '-F', 'babel.cfg', "-o", to_filename, "./"]
        reporter.report_debug_info('pybabel cmd line', cmdline)
        portable_popen(cmdline).wait()

translations = Translations() # let's have a singleton

class POFile(object):

    FILENAME_PATTERN = "contents+%s.po"

    def __init__(self, language, i18npath):
        self.language=language
        self.i18npath=i18npath

    def _exists(self):
        """Returns True if <language>.po file exists in i18npath"""
        filename=self.FILENAME_PATTERN%self.language
        return exists( join(self.i18npath, filename) )

    def _msg_init(self):
        """Generates the first <language>.po file"""
        msginit=locate_executable('msginit')
        cmdline=[msginit, "-i", "contents.pot", "-l", self.language, "-o", self.FILENAME_PATTERN%self.language, "--no-translator"]
        reporter.report_debug_info('msginit cmd line', cmdline)
        portable_popen(cmdline, cwd=self.i18npath).wait()

    def _msg_merge(self):
        """Merges an existing <language>.po file with .pot file"""
        msgmerge=locate_executable('msgmerge')
        cmdline=[msgmerge, self.FILENAME_PATTERN%self.language, "contents.pot", "-U", "-N", "--backup=simple"]
        reporter.report_debug_info('msgmerge cmd line', cmdline)
        portable_popen(cmdline, cwd=self.i18npath).wait()

    def _prepare_locale_dir(self):
        """Prepares the i18n/<language>/LC_MESSAGES/ to store the .mo file ; returns the dirname"""
        dirname = join('_compiled',self.language, "LC_MESSAGES")
        try:
            makedirs(join(self.i18npath,dirname))
        except OSError:
            pass # already exists, no big deal
        return dirname

    def _msg_fmt(self, locale_dirname):
        """Compile an existing <language>.po file into a .mo file"""
        msgfmt=locate_executable('msgfmt')
        cmdline=[msgfmt, self.FILENAME_PATTERN%self.language, "-o", join(locale_dirname,"contents.mo")]
        reporter.report_debug_info('msgfmt cmd line', cmdline)
        portable_popen(cmdline, cwd=self.i18npath).wait()

    def generate(self):
        if self._exists():
            self._msg_merge()
        else:
            self._msg_init()
        locale_dirname=self._prepare_locale_dir()
        self._msg_fmt(locale_dirname)

def _line_is_dashes(line):
    line = line.strip()
    return line == u'-' * len(line) and len(line) >= 3



class I18NPlugin(Plugin):
    name = u'i18n'
    description = u'Internationalisation helper'

    def translate_tag(self, s, *args, **kwargs):
        s=s.strip()
        if not self.enabled:
            return s
        ctx = get_ctx()
        if self.content_language==ctx.locale:
            translations.add(s,'(dynamic)')
            reporter.report_debug_info('added to translation memory (dynamic): ', truncate(s))
            return s
        else:
            translator = gettext.translation("contents", join(self.i18npath,'_compiled'), languages=[ctx.locale], fallback = True)
            return translator.ugettext(s)#.encode('utf-8')


    def choose_language(self, l, language, fallback='en', attribute='language'):
        """Will return from list 'l' the element with attribute 'attribute' set to given 'language'.
        If none is found, will try to return element with attribute 'attribute' set to given 'fallback'.
        Else returns None."""
        language=language.strip().lower()
        fallback=fallback.strip().lower()
        for item in l:
            if item[attribute].strip().lower()==language:
                return item
        # fallback
        for item in l:
            if item[attribute].strip().lower()==fallback:
                return item
        return None

    def on_setup_env(self):
        """Setup `env` for the plugin"""
        # Read configuration
        self.enabled = self.get_config().get('enable', 'true') in ('true','True','1')
        if not self.enabled:
            reporter.report_generic('I18N plugin disabled in configs/i18n.ini')

        self.i18npath = self.get_config().get('i18npath', 'i18n')
        self.url_prefix = self.get_config().get('url_prefix', 'http://localhost/')

        self.content_language=self.get_config().get('content', 'en')
        try:
            self.translations_languages=self.get_config().get('translations').replace(' ','').split(',')
        except AttributeError:
            raise RuntimeError('Please specify the "translations" configuration option in configs/i18n.ini')

        if not self.content_language in self.translations_languages:
            self.translations_languages.append(self.content_language)
        self.env.jinja_env.filters['translate'] = self.translate_tag
        self.env.jinja_env.globals['_'] = self.translate_tag
        self.env.jinja_env.globals['choose_language'] = self.choose_language

    def process_node(self, fields, sections, source, zone, root_path):
        """For a give node (), identify all fields to translate, and add new
        fields to translations memory. Flow blocks are handled recursively."""
        for field in fields:
            if ('translate' in field.options) \
            and (source.alt in (PRIMARY_ALT, self.content_language)) \
            and (field.options['translate'] in ('True', 'true', '1', 1)):
                if field.name in sections.keys():
                    section = sections[field.name]
                    for line in [x.strip() for x in section if x.strip()]:
                        translations.add(
                            line,
                            "%s (%s:%s.%s)" % (
                                urlparse.urljoin(self.url_prefix, source.url_path),
                                relpath(source.source_filename, root_path),
                                zone, field.name)
                            )

            if isinstance(field.type, FlowType):
                if sections.has_key(field.name):
                    section = sections[field.name]
                    for blockname, blockvalue in process_flowblock_data("".join(section)):
                        flowblockmodel = source.pad.db.flowblocks[blockname]
                        blockcontent=dict(tokenize(blockvalue))
                        self.process_node(flowblockmodel.fields, blockcontent, source, blockname, root_path)



    def on_before_build(self, builder, build_state, source, prog):
        """Before building a page, eventualy produce all its alternatives (=translated pages)
        using the gettext translations available."""
        # if isinstance(source,Page) and source.alt==PRIMARY_ALT:
        if self.enabled and isinstance(source,Page) and source.alt in (PRIMARY_ALT, self.content_language):
            contents = None
            for fn in source.iter_source_filenames():
                try:
                    contents=FileContents(fn)
                except IOError:
                    pass # next
            text = contents.as_text()
            fields = source.datamodel.fields
            sections = list(tokenize(text.splitlines())) # ('sectionname',[list of section texts])
            flowblocks = source.pad.db.flowblocks

            for language in self.translations_languages:
                translator = gettext.translation("contents", join(self.i18npath,'_compiled'), languages=[language], fallback = True)
                translated_filename=join(dirname(source.source_filename), "contents+%s.lr"%language)
                with open(translated_filename,"w") as f:
                    count_lines_block = 0 # counting the number of lines of the current block
                    is_content = False
                    for line in contents.open(encoding='utf-8').readlines():#text.splitlines():
                        stripped_line = line.strip()
                        if not stripped_line: # empty line
                            f.write('\n')
                            continue
                        if _line_is_dashes(stripped_line) or _block2_re.match(stripped_line): # line like "---*" or a new block tag
                            count_lines_block=0
                            is_content = False
                            f.write("%s"%line)
                        else:
                            count_lines_block+=1
                            if count_lines_block==1 and not is_content: # handle first line, while not in content
                                if _command_re.match(stripped_line):
                                    key,value=stripped_line.split(':',1)
                                    value=value.strip()
                                    if value:
                                        f.write( "%s: %s\n" % ( key.encode('utf-8'), translator.ugettext(value).encode('utf-8')  ))
                                    else:
                                        f.write( "%s:\n" % key.encode('utf-8') )
                            else:
                                is_content=True
                        if is_content:
                            translated_stripline = translator.ugettext(stripped_line) # trnanslate the stripped version
                            translation = line.replace(stripped_line, translated_stripline, 1) # and re-inject the stripped translation into original line (not stripped)
                            f.write( "%s"%translation.encode('utf-8') )


    def on_after_build(self, builder, build_state, source, prog):
        if self.enabled and isinstance(source,Page):
            try:
                text = source.contents.as_text()
            except IOError, e:
                pass
            else:
                fields = source.datamodel.fields
                sections = dict(tokenize(text.splitlines())) # {'sectionname':[list of section texts]}
                self.process_node(fields, sections, source, source.datamodel.id, builder.env.root_path)


    def on_before_build_all(self, builder, **extra):
        if self.enabled:
            reporter.report_generic("i18n activated, with main language %s"% self.content_language )
            templates_pot_filename = join(tempfile.gettempdir(), 'templates.pot')
            reporter.report_generic("Parsing templates for i18n into %s"% relpath(templates_pot_filename,builder.env.root_path) )
            translations.parse_templates(templates_pot_filename)


    def on_after_build_all(self, builder, **extra):
        """Once the build process is over :
        - write the translation template `contents.pot` on the filesystem,
        - write all translation contents+<language>.po files """
        if self.enabled:
            contents_pot_filename = join(builder.env.root_path, self.i18npath, 'contents.pot')
            templates_pot_filename = join(tempfile.gettempdir(), 'templates.pot')
            translations.write_pot(contents_pot_filename, self.content_language)
            reporter.report_generic("%s generated"%relpath(contents_pot_filename, builder.env.root_path))
            if exists(templates_pot_filename):
                translations.merge_pot([contents_pot_filename, templates_pot_filename], contents_pot_filename)
                reporter.report_generic("%s merged into %s"% (relpath(templates_pot_filename,builder.env.root_path),relpath(contents_pot_filename,builder.env.root_path)) )


            for language in self.translations_languages:
                po_file=POFile(language, self.i18npath)
                po_file.generate()


