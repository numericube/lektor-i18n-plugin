# -*- coding: utf-8 -*-
from lektor.pluginsystem import Plugin
from lektor.db import Page
from lektor.metaformat import tokenize
from lektor.reporter import reporter
from lektor.types.flow import FlowType, process_flowblock_data, discover_relevant_flowblock_models, _block_re
from lektor.utils import portable_popen, locate_executable
from lektor.environment import PRIMARY_ALT

from pprint import PrettyPrinter
from os.path import relpath, join, exists, dirname
from os import walk, makedirs
from datetime import datetime
import time, gettext
import fnmatch, re

_command_re = re.compile(r'([a-zA-Z0-9.-_]+):')


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
        if not source in self.translations[text]:
            self.translations[text].append(source)

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
        cmdline=[msgmerge, self.FILENAME_PATTERN%self.language, "contents.pot", "-U", "--backup=simple"]
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

    def on_setup_env(self):
        self.content_language=self.get_config().get('content', 'en')
        try:
            self.translations_languages=self.get_config().get('translations').replace(' ','').split(',')
        except AttributeError:
            raise RuntimeError('Please specify the "translations" configuration option in configs/i18n.ini')
        self.i18npath = self.get_config().get('i18npath', 'i18n')

    def process_node(self, fields, sections, source, zone, root_path):
        for field in fields:
            if ('translate' in field.options) \
            and (field.options['translate'] in ('True', 'true', '1', 1)):
                if field.name in sections.keys():
                    section = sections[field.name]
                    for line in [x.strip() for x in section if x.strip()]:
                        translations.add(
                            line,
                            "%s:%s.%s" % (
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
        if isinstance(source,Page) and source.alt==PRIMARY_ALT:
            text = source.contents.as_text()
            fields = source.datamodel.fields
            sections = list(tokenize(text.splitlines())) # ('sectionname',[list of section texts])
            flowblocks = source.pad.db.flowblocks


            for language in self.translations_languages:
                translator = gettext.translation("contents", join(self.i18npath,'_compiled'), languages=[language], fallback = True)
                translated_filename=join(dirname(source.source_filename), "contents+%s.lr"%language)
                with open(translated_filename,"w") as f:
                    count_lines_block = 0 # counting the number of lines of the current block
                    is_content = False
                    for line in source.contents.open(encoding='utf-8').readlines():#text.splitlines():
                        stripped_line = line.strip()
                        if not stripped_line:
                            f.write('\n')
                            continue
                        if _line_is_dashes(stripped_line) or _block_re.match(stripped_line):
                            count_lines_block=0
                            is_content = False
                            f.write("%s"%line)
                        else:
                            count_lines_block+=1
                            if count_lines_block==1 and not is_content:
                                if _command_re.match(stripped_line):
                                    key,value=stripped_line.split(':',1)
                                    value=value.strip()
                                    if value:
                                        try:
                                            f.write("%s: %s\n"%(key.encode('utf-8'), translator.ugettext(value).encode('utf-8')  ))
                                        except UnicodeError:
                                            import ipdb; ipdb.set_trace()
                                    else:
                                        f.write("%s:\n"%key.encode('utf-8'))
                            else:
                                is_content=True
                        if is_content:
                            f.write("%s"%translator.gettext(line).encode('utf-8') )


    def on_after_build(self, builder, build_state, source, prog):
        if isinstance(source,Page):
            try:
                text = source.contents.as_text()
            except IOError:
                pass
            else:
                fields = source.datamodel.fields
                sections = dict(tokenize(text.splitlines())) # {'sectionname':[list of section texts]}
                self.process_node(fields, sections, source, source.datamodel.id, builder.env.root_path)


    def on_after_build_all(self, builder, **extra):
        pot_filename = join(builder.env.root_path, self.i18npath, 'contents.pot')
        with open(pot_filename,'w') as f:
            f.write(translations.as_pot(self.content_language).encode("utf-8"))
        reporter.report_generic("%s generated"%pot_filename)

        for language in self.translations_languages:
            po_file=POFile(language, self.i18npath)
            po_file.generate()
