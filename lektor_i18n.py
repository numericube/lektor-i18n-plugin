import collections
import copy
import gettext
import os
import re
import tempfile
from datetime import datetime
from datetime import timezone
from os.path import dirname
from os.path import exists
from os.path import join
from os.path import relpath
from pprint import PrettyPrinter
from urllib.parse import urljoin

from lektor.context import get_ctx
from lektor.datamodel import load_flowblocks
from lektor.db import Page
from lektor.environment import PRIMARY_ALT
from lektor.metaformat import serialize
from lektor.metaformat import tokenize
from lektor.pluginsystem import Plugin
from lektor.reporter import reporter
from lektor.types.flow import FlowType
from lektor.types.flow import process_flowblock_data
from lektor.utils import locate_executable
from lektor.utils import portable_popen

POT_HEADER = """msgid ""
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

# regexp pattern matching prefix characters in markdown headings and lists
# which we want to exclude from translation strings
HL_PATTERN = re.compile(r"^\s*#+\s*|^\s*[*-]\s+")


def truncate(s, length=32):
    return (s[:length] + "..") if len(s) > length else s


class TemplateTranslator:
    def __init__(self, i18npath):
        self.i18npath = i18npath
        self.__lastlang = None
        self.translator = None
        self.init_translator()

    def init_translator(self):
        ctx = get_ctx()
        if not ctx:
            self.translator = gettext.GNUTranslations()
            return super().__init__()
        if not self.__lastlang == ctx.locale:
            self.__lastlang = ctx.locale
            self.translator = gettext.translation(
                "contents",
                join(self.i18npath, "_compiled"),
                languages=[ctx.locale],
                fallback=True,
            )

    def gettext(self, x):
        self.init_translator()  # lagnuage could have changed
        return self.translator.gettext(x)

    def ngettext(self, *x):
        self.init_translator()
        return self.translator.ngettext(*x)


class Translations:
    """Memory of translations"""

    def __init__(self):
        # dict like {'text' : ['source1', 'source2',...],}
        self.translations = collections.OrderedDict()

    def add(self, text, source):
        if text not in self.translations.keys():
            self.translations[text] = []
            reporter.report_debug_info("added to translation memory", truncate(text))
        if source not in self.translations[text]:
            self.translations[text].append(source)

    def __repr__(self):
        return PrettyPrinter(2).pformat(self.translations)

    def as_pot(self, content_language):
        """returns a POT version of the translation dictionnary"""
        now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M %Z")
        result = POT_HEADER % {"LANGUAGE": content_language, "NOW": now}

        for msg, paths in self.translations.items():
            result += "#: {}\n".format(" ".join(paths))
            for token, repl in {
                "\\": "\\\\",
                "\n": "\\n",
                "\t": "\\t",
                '"': '\\"',
            }.items():
                msg = msg.replace(token, repl)
            result += f'msgid "{msg}"\n'
            result += 'msgstr ""\n\n'
        return result

    def write_pot(self, pot_filename, language):
        if not os.path.exists(dirname(pot_filename)):
            os.makedirs(dirname(pot_filename))
        with open(pot_filename, "w") as f:
            f.write(self.as_pot(language))

    def merge_pot(self, from_filenames, to_filename):
        msgcat = locate_executable("msgcat")
        cmdline = [msgcat]
        cmdline.extend(from_filenames)
        cmdline.extend(("-o", to_filename))
        reporter.report_debug_info("msgcat cmd line", cmdline)
        portable_popen(cmdline).wait()

    def parse_templates(self, to_filename):
        pybabel = locate_executable("pybabel")
        cmdline = [pybabel, "extract", "-F", "babel.cfg", "-o", to_filename, "./"]
        reporter.report_debug_info("pybabel cmd line", cmdline)
        portable_popen(cmdline).wait()


translations = Translations()  # let's have a singleton


class POFile:
    FILENAME_PATTERN = "contents+%s.po"

    def __init__(self, language, i18npath):
        self.language = language
        self.i18npath = i18npath

    def _exists(self):
        """Returns True if <language>.po file exists in i18npath"""
        filename = self.FILENAME_PATTERN % self.language
        return exists(join(self.i18npath, filename))

    def _msg_init(self):
        """Generates the first <language>.po file"""
        msginit = locate_executable("msginit")
        cmdline = [
            msginit,
            "-i",
            "contents.pot",
            "-l",
            self.language,
            "-o",
            self.FILENAME_PATTERN % self.language,
            "--no-translator",
        ]
        reporter.report_debug_info("msginit cmd line", cmdline)
        portable_popen(cmdline, cwd=self.i18npath).wait()

    def _msg_merge(self):
        """Merges an existing <language>.po file with .pot file"""
        msgmerge = locate_executable("msgmerge")
        cmdline = [
            msgmerge,
            self.FILENAME_PATTERN % self.language,
            "contents.pot",
            "-U",
            "-N",
            "--backup=simple",
        ]
        reporter.report_debug_info("msgmerge cmd line", cmdline)
        portable_popen(cmdline, cwd=self.i18npath).wait()

    def _prepare_locale_dir(self):
        """Prepares the i18n/<language>/LC_MESSAGES/ to store the .mo file;
        returns the dirname"""
        directory = join("_compiled", self.language, "LC_MESSAGES")
        try:
            os.makedirs(join(self.i18npath, directory))
        except OSError:
            pass  # already exists, no big deal
        return directory

    def _msg_fmt(self, locale_dirname):
        """Compile an existing <language>.po file into a .mo file"""
        msgfmt = locate_executable("msgfmt")
        cmdline = [
            msgfmt,
            self.FILENAME_PATTERN % self.language,
            "-o",
            join(locale_dirname, "contents.mo"),
        ]
        reporter.report_debug_info("msgfmt cmd line", cmdline)
        portable_popen(cmdline, cwd=self.i18npath).wait()

    def generate(self):
        if self._exists():
            self._msg_merge()
        else:
            self._msg_init()

    def compile(self):
        if self._exists():
            locale_dirname = self._prepare_locale_dir()
            self._msg_fmt(locale_dirname)


def split_paragraphs(document):
    if isinstance(document, (list, tuple)):
        document = "".join(document)  # list of lines
    return re.split("\n(?:\\s*\n){1,}", document)


class I18NPlugin(Plugin):
    name = "i18n"
    description = "Internationalisation helper"

    def translate_tag(self, s, *args, **kwargs):
        if not self.enabled:
            return s  # no operation
        s = s.strip()
        ctx = get_ctx()
        if self.content_language == ctx.locale:
            return s
        else:
            translator = gettext.translation(
                "contents",
                join(self.i18npath, "_compiled"),
                languages=[ctx.locale],
                fallback=True,
            )
            return translator.gettext(s)

    def choose_language(self, items, language, fallback="en", attribute="language"):
        """Will return from list 'item_list' the element with attribute 'attribute' set to given 'language'.
        If none is found, will try to return element with attribute 'attribute' set to given 'fallback'.
        Else returns None."""  # noqa: E501
        language = language.strip().lower()
        fallback = fallback.strip().lower()
        for item in items:
            if item[attribute].strip().lower() == language:
                return item
        # fallback
        for item in items:
            if item[attribute].strip().lower() == fallback:
                return item
        return None

    def on_setup_env(self, **extra):
        """Setup `env` for the plugin"""
        # Read configuration
        self.enabled = self.get_config().get("enable", "true") in ("true", "True", "1")
        if not self.enabled:
            reporter.report_generic("I18N plugin disabled in configs/i18n.ini")

        self.i18npath = self.get_config().get("i18npath", "i18n")
        self.url_prefix = self.get_config().get("url_prefix", "http://localhost/")
        # whether or not to use a pargraph as smallest translatable unit
        self.trans_parwise = self.get_config().get(
            "translate_paragraphwise", "false"
        ) in ("true", "True", "1")
        self.content_language = self.get_config().get("content", "en")
        self.env.jinja_env.add_extension("jinja2.ext.i18n")
        self.env.jinja_env.policies["ext.i18n.trimmed"] = True  # do a .strip()
        self.env.jinja_env.install_gettext_translations(
            TemplateTranslator(self.i18npath)
        )
        # ToDo: is this still required
        try:
            self.translations_languages = (
                self.get_config().get("translations").replace(" ", "").split(",")
            )
        except AttributeError:
            msg = (
                "Please specify the 'translations' configuration option ",
                "in configs/i18n.ini",
            )
            raise RuntimeError(msg)

        if self.content_language not in self.translations_languages:
            self.translations_languages.append(self.content_language)

        self.env.jinja_env.filters["translate"] = self.translate_tag
        self.env.jinja_env.globals["_"] = self.translate_tag
        self.env.jinja_env.globals["choose_language"] = self.choose_language

    def process_node(self, fields, sections, source, zone, root_path):
        """For a given node (), identify all fields to translate, and add new
        fields to translations memory. Flow blocks are handled recursively."""
        source_filename = source.source_filename.replace(f"+{self.content_language}.lr",
                                                         ".lr")
        source_relpath = relpath(source_filename, root_path)
        for field in fields:
            if (
                ("translate" in field.options)
                and (source.alt in (PRIMARY_ALT, self.content_language))
                and (field.options["translate"] in ("True", "true", "1", 1))
            ):
                if field.name in sections.keys():
                    section = sections[field.name]
                    # if blockwise, each paragraph is one translatable message,
                    # otherwise each line
                    if self.trans_parwise:
                        chunks = split_paragraphs(section)
                    else:
                        chunks = []
                        for line in section:
                            line_stripped = re.sub(HL_PATTERN, "", line.strip())
                            if line_stripped:
                                chunks.append(line_stripped)
                    for chunk in chunks:
                        translation_source = f"{source_relpath}:{zone}.{field.name}"
                        translation_url = urljoin(self.url_prefix, source.url_path)
                        translations.add(
                            chunk.strip("\r\n"),
                            f"{translation_url} {translation_source}",
                        )

            if isinstance(field.type, FlowType):
                if field.name in sections:
                    section = sections[field.name]
                    for blockname, blockvalue in process_flowblock_data(
                        "".join(section)
                    ):
                        flowblockmodel = source.pad.db.flowblocks[blockname]
                        blockcontent = dict(tokenize(blockvalue))
                        self.process_node(
                            flowblockmodel.fields,
                            blockcontent,
                            source,
                            blockname,
                            root_path,
                        )

    def translate_flowblock(self, field, field_content, language, flowblocks, nested=0):
        """Iterate recursively into flowblock,
        returning in serialized format with field values translated"""
        ret = []
        for blockname, blockvalue in process_flowblock_data("".join(field_content)):
            blocksep = "####" + "#" * nested
            ret.append(f"{blocksep} {blockname} {blocksep}\n")
            flowblockmodel = flowblocks[blockname]
            flowblock_content = dict(tokenize(blockvalue))
            flowblock_content_translated = {}
            for flowblock_field in flowblockmodel.fields:
                if flowblock_field.name in flowblock_content:
                    flowblock_field_content = "\n".join(
                        [
                            item.strip()
                            for item in flowblock_content[flowblock_field.name]
                            if item
                        ]
                    )
                    flowblock_content_translated[flowblock_field.name] = (
                        self.translate_field(
                            flowblock_field,
                            flowblock_field_content,
                            language,
                            flowblocks,
                            nested + 1,
                        )
                    )
            for line in serialize(flowblock_content_translated.items()):
                ret.append(line)
        return "".join(ret).strip()

    def translate_field(self, field, field_content, language, flowblocks, nested=0):
        """Return the value for a field, translated if enabled for this field"""
        if ("translate" in field.options) and field.options["translate"] in (
            "True",
            "true",
            "1",
            1,
        ):
            translator = gettext.translation(
                "contents",
                join(self.i18npath, "_compiled"),
                languages=[language],
                fallback=True,
            )
            if self.trans_parwise:
                return self.__trans_parwise(field_content, translator)
            else:
                return self.__trans_linewise(field_content, translator)
        elif isinstance(field.type, FlowType):
            return self.translate_flowblock(
                field, field_content, language, flowblocks, nested
            )
        else:
            return field_content

    def get_instance(self, pad, root, content, children_models):
        """Returns a Page instance for content file"""
        rv = {}
        rv["_path"] = os.path.join(root, "contents.lr")
        rv["_alt"] = PRIMARY_ALT
        if "_model" not in content:
            path = os.path.dirname(root.rstrip("/"))
            while path:
                if children_models.get(path):
                    rv["_model"] = children_models[path]
                    break
                path = os.path.dirname(path.rstrip("/"))
        return pad.instance_from_data(rv | content)

    def translate_contents(self, builder):
        """Produce all content file alternatives (=translated pages)
        using the gettext translations available."""
        children_models = {}
        flowblocks = load_flowblocks(self.env)
        for root, _, files in os.walk(os.path.join(self.env.root_path, "content")):
            if re.match("content$", root):
                continue
            if "contents.lr" in files:
                fn = os.path.join(root, "contents.lr")
                content = {}
                content_alt = {}

                with open(fn, "rb") as f:
                    for key, lines in tokenize(f, encoding="utf-8"):
                        content[key] = "".join(lines)

                instance = self.get_instance(
                    builder.pad, root, content, children_models
                )

                if instance.datamodel.child_config.model:
                    children_models[root] = instance.datamodel.child_config.model

                for language in self.translations_languages:
                    content_alt[language] = copy.copy(content)
                    if language != self.content_language:
                        for field in instance.datamodel.fields:
                            if field.name in content.keys():
                                content_alt[language][field.name] = (
                                    self.translate_field(
                                        field, content[field.name], language, flowblocks
                                    )
                                )
                    translated_filename = os.path.join(root, f"contents+{language}.lr")
                    with open(translated_filename, "wb") as f:
                        for line in serialize(
                            content_alt[language].items(), encoding="utf-8"
                        ):
                            f.write(line)

    def __trans_linewise(self, content, translator):
        """Translate the chunk linewise."""
        lines = []
        for line in content.split("\n"):
            line_stripped = re.sub(HL_PATTERN, "", line.strip())
            trans_stripline = ""
            if line_stripped:
                trans_stripline = translator.gettext(
                    line_stripped
                )  # translate the stripped version
            # and re-inject the stripped translation into original line (not stripped)
            lines.append(line.replace(line_stripped, trans_stripline, 1))
        return "\n".join(lines)

    def __trans_parwise(self, content, translator):
        """Extract translatable strings block-wise, query for translation of
        block and re-inject result."""
        result = []
        for paragraph in split_paragraphs(content):
            stripped = paragraph.strip("\n\r")
            paragraph = paragraph.replace(stripped, translator.gettext(stripped))
            result.append(paragraph)
        return "\n\n".join(result)

    def on_after_build(self, builder, build_state, source, prog, **extra):
        if self.enabled and isinstance(source, Page):
            try:
                text = source.contents.as_text()
            except OSError:
                pass
            else:
                fields = source.datamodel.fields
                sections = dict(
                    tokenize(text.splitlines())
                )  # {'sectionname':[list of section texts]}
                self.process_node(
                    fields, sections, source, source.datamodel.id, builder.env.root_path
                )

    def get_templates_pot_filename(self):
        try:
            return self.pot_templates_filename
        except AttributeError:
            self.pot_templates_file = tempfile.NamedTemporaryFile(
                suffix=".pot", prefix="templates-"
            )
            self.pot_templates_filename = self.pot_templates_file.name
            return self.pot_templates_filename

    def on_before_build_all(self, builder, **extra):
        if self.enabled:
            reporter.report_generic(
                f"i18n activated, with main language {self.content_language}"
            )
            templates_pot_filename = self.get_templates_pot_filename()
            templates_relpath = relpath(templates_pot_filename, builder.env.root_path)
            reporter.report_generic(
                f"Parsing templates for i18n into {templates_relpath}"
            )
            translations.parse_templates(templates_pot_filename)
            # compile existing po files
            for language in self.translations_languages:
                po_file = POFile(language, self.i18npath)
                po_file.compile()
            # walk through contents.lr files and produce alternatives
            # before the build system creates its work queue
            self.translate_contents(builder)

    def on_after_build_all(self, builder, **extra):
        """Once the build process is over :
        - write the translation template `contents.pot` on the filesystem,
        - write all translation contents+<language>.po files"""
        if not self.enabled:
            return
        contents_pot_filename = join(
            builder.env.root_path, self.i18npath, "contents.pot"
        )
        pots = [
            contents_pot_filename,
            self.get_templates_pot_filename(),
            join(builder.env.root_path, self.i18npath, "plugins.pot"),
        ]
        # write out contents.pot from web site contents
        translations.write_pot(pots[0], self.content_language)
        reporter.report_generic(f"{relpath(pots[0], builder.env.root_path)} generated")
        pots = [p for p in pots if os.path.exists(p)]  # only keep existing ones
        if len(pots) > 1:
            translations.merge_pot(pots, contents_pot_filename)
            reporter.report_generic(
                "Merged POT files {}".format(
                    ", ".join(relpath(p, builder.env.root_path) for p in pots)
                )
            )

        for language in self.translations_languages:
            po_file = POFile(language, self.i18npath)
            po_file.generate()

        self.pot_templates_file.close()
