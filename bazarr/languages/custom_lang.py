# -*- coding: utf-8 -*-

import logging
import os
import re

from subzero.language import Language

from app.database import database, insert, update
from sqlalchemy.exc import IntegrityError

logger = logging.getLogger(__name__)


class CustomLanguage:
    """Base class for custom languages."""

    alpha2 = "pb"
    alpha3 = "pob"
    language = "pt-BR"
    official_alpha2 = "pt"
    official_alpha3 = "por"
    name = "Portuguese (Brazil)"
    iso = "BR"
    # The tag a translated file is named with when it targets this language.
    # None names the file with the subzero language's own basename, as before;
    # a language whose region is a UN M.49 code has no such basename (no
    # babelfish country or script exists for it), so it names the tag here.
    regional_tag = None
    _scripts = []
    _possible_matches = ("pt-br", "pob", "pb", "brazilian", "brasil", "brazil")
    # Tokens too short or too ordinary to be safe as a substring of any title:
    # they match only as a whole word. "tc" as a substring otherwise
    # classifies "Dutch" as Traditional Chinese.
    _word_only_matches = ()
    # Words that may sit directly against a word-only token in one run-together
    # word ("ChineseTraditional"); digits after the token are always allowed
    # ("CHT1"). Anything else against the token is another word.
    _word_compound_affixes = ()
    _extensions = (".pt-br", ".pob", ".pb")
    _extensions_forced = (".pt-br.forced", ".pob.forced", ".pb.forced")
    _extensions_hi = (".pt-br.hi", ".pob.hi", ".pb.hi",
                      ".pt-br.cc", ".pob.cc", ".pb.cc",
                      ".pt-br.sdh", ".pob.sdh", ".pb.sdh")

    def subzero_language(self):
        return Language(self.official_alpha3, self.iso)

    @classmethod
    def from_value(cls, value, attr="alpha3"):
        """Return a custom language subclass by value and attribute
        if found, otherwise return None.

        :param value:
        :param attr:
        """
        for sub in cls.__subclasses__():
            if getattr(sub, attr) == str(value):
                return sub()

        return None

    @classmethod
    def register(cls, table):
        """Register the custom language subclasses in the database."""

        for sub in cls.__subclasses__():
            try:
                database.execute(
                    insert(table)
                    .values(code3=sub.alpha3,
                            code2=sub.alpha2,
                            name=sub.name,
                            enabled=0))
            except IntegrityError:
                database.execute(
                    update(table)
                    .values(code2=sub.alpha2,
                            name=sub.name)
                    .where(table.code3 == sub.alpha3))

    @classmethod
    def found_external(cls, subtitle, subtitle_path):
        for sub in cls.__subclasses__():
            code = sub.get_alpha_type(subtitle, subtitle_path)
            if code:
                return code

        return None

    @classmethod
    def get_alpha_type(cls, subtitle: str, subtitle_path=None):

        extension = str(os.path.splitext(subtitle)[0]).lower()
        to_return = None

        if extension.endswith(cls._extensions):
            to_return = cls.alpha2

        if extension.endswith(cls._extensions_forced):
            to_return = f"{cls.alpha2}:forced"

        if extension.endswith(cls._extensions_hi):
            to_return = f"{cls.alpha2}:HI"

        if to_return is not None:
            logging.debug("BAZARR external subtitles detected: %s", to_return)

        return to_return

    def ffprobe_found(self, detected_language: dict) -> bool:
        name = detected_language.get("name", "").lower()
        if not name:
            return False

        words = None
        for token in self._possible_matches:
            if token in self._word_only_matches:
                if words is None:
                    words = set(re.split(r'[^a-z0-9]+', name))
                if token in words:
                    return True
                affixes = '|'.join(re.escape(affix) for affix in self._word_compound_affixes)
                affix_group = f'(?:{affixes})?' if affixes else ''
                compound = re.compile(f'{affix_group}{re.escape(token)}{affix_group}[0-9]*')
                if any(compound.fullmatch(word) for word in words):
                    return True
            elif token in name:
                return True
        return False

    def language_found(self, language: Language):
        if str(language.country) == self.iso:
            return True

        if language.script and str(language.script) in self._scripts:
            return True

        return False


class BrazilianPortuguese(CustomLanguage):
    # Same attributes as base class
    pass


class Portuguese(CustomLanguage):
    alpha2 = "pt"
    alpha3 = "por"
    language = "pt-PT"
    official_alpha2 = "pt"
    official_alpha3 = "por"
    name = "Portuguese"
    iso = "PT"
    _scripts = []
    _possible_matches = ("pt-pt", "por", "pt")
    _extensions = (".pt-pt", ".por", ".pt")
    _extensions_forced = (".pt-pt.forced", ".por.forced", ".pt.forced")
    _extensions_hi = (".pt-pt.hi", ".por.hi", ".pt.hi",
                      ".pt-pt.cc", ".por.cc", ".pt.cc",
                      ".pt-pt.sdh", ".por.sdh", ".pt.sdh")

    def subzero_language(self):
        return Language(self.official_alpha3)

    def language_found(self, language: Language):
        return str(language.alpha3) == self.alpha3


class ChineseTraditional(CustomLanguage):
    alpha2 = "zt"
    alpha3 = "zht"
    language = "zh-TW"
    official_alpha2 = "zh"
    official_alpha3 = "zho"
    name = "Chinese Traditional"
    iso = "TW"
    # _scripts = (Script("Hant"),)
    # We'll use literals for now
    _scripts = ("Hant",)
    # The base class default is the Brazilian Portuguese list, which left this
    # subclass unable to detect a Traditional track by its ffprobe title and
    # able to misdetect a track whose title happened to contain "pb" or
    # "brasil". The tokens below mirror the conventions the _extensions and the
    # catalog's embeddedsubtitles provider already encode. There is no bare
    # "繁" and no "繁中": the mixed-script label "简繁中文" carries both, and
    # this matcher is a plain substring test with no disambiguation step, so
    # only unambiguous tokens qualify.
    _possible_matches = (
        "zh-tw", "zh-hant", "zhhant", "zht", "hant", "big5", "cht", "tc",
        "traditional", "taiwan", "繁體", "繁体", "正體",
    )
    # "tc" is the one two-letter token here, and a substring test would make
    # "Dutch" Traditional Chinese. "hant", "cht" and "traditional" sit inside
    # ordinary words too ("Shantaram", "Yacht", "nontraditional"). All four
    # match as a word only ("TC", "TC.1080p", "zh-Hant", "Traditional Chinese").
    # The compound forms (zh-tw, zh-hant, zhhant) stay substring-matched.
    _word_only_matches = ("tc", "hant", "cht", "traditional")
    # "ChineseTraditional", "TraditionalChinese", "CHT1" and "Hant2" are
    # written as one word; "Merchant1" and "Nontraditional1" are not them.
    _word_compound_affixes = ("chinese",)
    _extensions = (
        ".cht", ".tc", ".zh-tw", ".zht", ".zh-hant", ".zhhant", ".zh_hant", ".hant", ".big5", ".traditional",
    )
    _extensions_forced = (
        ".cht.forced", ".tc.forced", ".zht.forced", "hant.forced", ".big5.forced", "繁體中文.forced", "雙語.forced",
        ".zh-tw.forced",
    )
    _extensions_hi = (
        ".cht.hi", ".tc.hi", ".zht.hi", "hant.hi", ".big5.hi", "繁體中文.hi", "雙語.hi", ".zh-tw.hi",
        ".cht.cc", ".tc.cc", ".zht.cc", "hant.cc", ".big5.cc", "繁體中文.cc", "雙語.cc", ".zh-tw.cc",
        ".cht.sdh", ".tc.sdh", ".zht.sdh", "hant.sdh", ".big5.sdh", "繁體中文.sdh", "雙語.sdh", ".zh-tw.sdh",
    )
    _extensions_fuzzy = ("繁", "雙語")
    _extensions_disamb_fuzzy = ("简", "双语")
    _extensions_disamb = (
        ".chs", ".sc", ".zhs", ".zh-hans", ".hans", ".zh_hans", ".zhhans", ".gb", ".simplified",
    )
    _extensions_disamb_forced = (
        ".chs.forced", ".sc.forced", ".zhs.forced", "hans.forced", ".gb.forced", "简体中文.forced", "双语.forced",
    )
    _extensions_disamb_hi = (
        ".chs.hi", ".sc.hi", ".zhs.hi", "hans.hi", ".gb.hi", "简体中文.hi", "双语.hi",
        ".chs.cc", ".sc.cc", ".zhs.cc", "hans.cc", ".gb.cc", "简体中文.cc", "双语.cc",
        ".chs.sdh", ".sc.sdh", ".zhs.sdh", "hans.sdh", ".gb.sdh", "简体中文.sdh", "双语.sdh",
    )

    @classmethod
    def get_alpha_type(cls, subtitle, subtitle_path=None):
        subtitle_path = str(subtitle_path).lower()
        extension = str(os.path.splitext(subtitle)[0]).lower()

        to_return = None

        # Simplified chinese
        if (
            extension.endswith(cls._extensions_disamb)
            or subtitle_path in cls._extensions_disamb_fuzzy
        ):
            to_return = "zh"

        elif any(ext in extension[-12:] for ext in cls._extensions_disamb_forced):
            to_return = "zh:forced"

        elif any(ext in extension[-12:] for ext in cls._extensions_disamb_hi):
            to_return = "zh:HI"

        # Traditional chinese
        elif (
            extension.endswith(cls._extensions)
            or subtitle_path[:-5] in cls._extensions_fuzzy
        ):
            to_return = "zt"

        elif any(ext in extension[-12:] for ext in cls._extensions_forced):
            to_return = "zt:forced"

        elif any(ext in extension[-12:] for ext in cls._extensions_hi):
            to_return = "zt:HI"

        if to_return is not None:
            logging.debug("BAZARR external subtitles detected: %s", to_return)

        return to_return


class LatinAmericanSpanish(CustomLanguage):
    alpha2 = "ea"  # Only one available I can think of
    alpha3 = "spl"
    language = "es-MX"
    official_alpha2 = "es"
    official_alpha3 = "spa"
    name = "Spanish (Latino)"
    iso = "MX"  # Not fair, but ok
    _scripts = ("419",)
    # The IETF tag for Latin American Spanish (UN M.49 region 419): the tag a
    # translation names its file with, since no babelfish country or script
    # exists for the region and no subzero language can carry it as a basename.
    regional_tag = "es-419"
    _possible_matches = (
        "es-la", "spa-la", "spl", "mx", "latin", "mexic", "argent", "latam",
    )
    _extensions = (".es-la", ".spl", ".spa-la", ".ea", ".es-mx", ".lat", ".es.ar", ".es-419")
    _extensions_forced = (
        ".es-la.forced", ".spl.forced", ".spa-la.forced", ".ea.forced", ".es-mx.forced", ".lat.forced", ".es.ar.forced",
        ".es-419.forced",
    )
    _extensions_hi = (
        ".es-la.hi", ".spl.hi", ".spa-la.hi", ".ea.hi", ".es-mx.hi", ".lat.hi", ".es.ar.hi", ".es-419.hi",
        ".es-la.cc", ".spl.cc", ".spa-la.cc", ".ea.cc", ".es-mx.cc", ".lat.cc", ".es.ar.cc", ".es-419.cc",
        ".es-la.sdh", ".spl.sdh", ".spa-la.sdh", ".ea.sdh", ".es-mx.sdh", ".lat.sdh", ".es.ar.sdh", ".es-419.sdh",
    )
