"""

    Reynir: Natural language processing for Icelandic

    BIN parser module

    Copyright (C) 2016 Vilhjálmur Þorsteinsson

       This program is free software: you can redistribute it and/or modify
       it under the terms of the GNU General Public License as published by
       the Free Software Foundation, either version 3 of the License, or
       (at your option) any later version.
       This program is distributed in the hope that it will be useful,
       but WITHOUT ANY WARRANTY; without even the implied warranty of
       MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
       GNU General Public License for more details.

    You should have received a copy of the GNU General Public License
    along with this program.  If not, see http://www.gnu.org/licenses/.


    This module implements the BIN_Token class, deriving from Token,
    and BIN_Parser class, deriving from Parser.
    BIN_Parser parses sentences in Icelandic according to the grammar
    in the file Reynir.grammar.

    BIN refers to 'Beygingarlýsing íslensks nútímamáls', the database of
    word forms in modern Icelandic.

"""

import os
import time

from datetime import datetime
from functools import reduce
import json

from settings import Settings, VerbObjects, VerbSubjects, Prepositions
from tokenizer import TOK
from bindb import BIN_Db
from grammar import Terminal, LiteralTerminal, Nonterminal, Token, Grammar, GrammarError
from baseparser import Base_Parser

from flask import current_app


def debug():
    # Call this to trigger the Flask debugger on purpose
    assert current_app.debug == False, "Don't panic! You're here by request of debug()"


class BIN_Token(Token):

    """
        Wrapper class for a token to be processed by the parser

        The layout of a token tuple coming from the tokenizer is
        as follows:

        t[0] Token type (TOK.WORD, etc.)
        t[1] Token text
        t[2] For TOK.WORD: Meaning list, where each item is a tuple:
            m[0] Word stem
            m[1] BIN index (integer)
            m[2] Word type (kk/kvk/hk (=noun), so, lo, ao, fs, etc.)
            m[3] Word category (alm/fyr/ism etc.)
            m[4] Word form (in most cases identical to t[1])
            m[5] Grammatical form (case, gender, number, etc.)

    """

    # Map word types to those used in the grammar
    _KIND = {
        "kk": "no",
        "kvk": "no",
        "hk": "no",
        "so": "so",
        "ao": "ao",
        "fs": "fs",
        "lo": "lo",
        "fn": "fn",
        "pfn": "pfn",
        "gr": "gr",
        "to": "to",
        "töl": "töl",
        "uh": "uh",
        "st": "st",
        "abfn": "abfn",
        "nhm": "nhm"
    }

    # Strings that must be present in the grammatical form for variants
    VARIANT = {
        "nf" : "NF", # Nefnifall / nominative
        "þf" : "ÞF", # Þolfall / accusative
        "þgf" : "ÞGF", # Þágufall / dative
        "ef" : "EF", # Eignarfall / possessive
        "kk" : "KK", # Karlkyn / masculine
        "kvk" : "KVK", # Kvenkyn / feminine
        "hk" : "HK", # Hvorugkyn / neutral
        "et" : "ET", # Eintala / singular
        "ft" : "FT", # Fleirtala / plural
        "mst" : "MST", # Miðstig / comparative
        "esb" : "ESB", # Efsta stig, sterk beyging / superlative
        "evb" : "EVB", # Efsta stig, veik beyging / superlative
        "p1" : "1P", # Fyrsta persóna / first person
        "p2" : "2P", # Önnur persóna / second person
        "p3" : "3P", # Þriðja persóna / third person
        "op" : "OP", # Ópersónuleg sögn
        "gm" : "GM", # Germynd
        "mm" : "MM", # Miðmynd
        "sb" : "SB", # Sterk beyging
        "vb" : "VB", # Veik beyging
        "nh" : "NH", # Nafnháttur
        "fh" : "FH", # Framsöguháttur
        "bh" : "BH", # Boðháttur
        "lh" : "LH", # Lýsingarháttur (nútíðar)
        "vh" : "VH", # Viðtengingarháttur
        "nt" : "NT", # Nútíð
        # "þt" : "ÞT", # Þátíð # !!! Conflict with LHÞT
        "sagnb" : "SAGNB", # Sagnbót ('vera' -> 'hefur verið')
        "lhþt" : "LHÞT", # Lýsingarháttur þátíðar ('var lentur')
        "gr" : "gr", # Greinir

        # Variants that do not have a corresponding BIN meaning
        "abbrev" : None,
        "subj" : None
    }

    # Bit mapping for all known variants
    VBIT = { key : 1 << i for i, key in enumerate(VARIANT.keys()) }
    # Bit mapping for all variants that have a corresponding BIN meaning
    FBIT = { val : 1 << i for i, val in enumerate(VARIANT.values()) if val }

    VBIT_ET = VBIT["et"]
    VBIT_FT = VBIT["ft"]
    VBIT_KK = VBIT["kk"]
    VBIT_KVK = VBIT["kvk"]
    VBIT_HK = VBIT["hk"]
    VBIT_NH = VBIT["nh"]
    VBIT_VH = VBIT["vh"]
    VBIT_LH = VBIT["lhþt"]
    VBIT_MM = VBIT["mm"]
    VBIT_GM = VBIT["gm"]
    VBIT_GR = VBIT["gr"]
    VBIT_SUBJ = VBIT["subj"]
    VBIT_SAGNB = VBIT["sagnb"]
    VBIT_ABBREV = VBIT["abbrev"]

    # Mask the following bits off a VBIT set to get an FBIT set
    FBIT_MASK = VBIT_ABBREV | VBIT_SUBJ

    CASES = ["nf", "þf", "þgf", "ef"]
    GENDERS = ["kk", "kvk", "hk"]
    GENDERS_SET = frozenset(GENDERS)
    GENDERS_MAP = { "kk" : "KK", "kvk" : "KVK", "hk" : "HK" }

    VBIT_CASES = VBIT["nf"] | VBIT["þf"] | VBIT["þgf"] | VBIT["ef"]
    VBIT_GENDERS = VBIT["kk"] | VBIT["kvk"] | VBIT["hk"]

    # Variants to be checked for verbs
    VERB_VARIANTS = ["p1", "p2", "p3", "nh", "vh", "lh", "bh", "fh",
        "sagnb", "lhþt", "nt", "kk", "kvk", "hk", "sb", "vb", "gm", "mm"]
    # Pre-calculate a dictionary of associated BIN forms
    _VERB_FORMS = None # Initialized later

    # Set of adverbs that cannot be an "eo" (prepositions and pronouns are already excluded)
    _NOT_EO = frozenset(["og", "eða", "sem", "ekkert"])

    # Prepositions that nevertheless must be allowed as adverbs
    # 'Fyrirtækið hefur skilað inn ársreikningi', 'Þá er kannski eftir klukkutími',
    # 'Það hafi opnað fyrir of ágenga nýtingu þeirra'
    # '...verður ekki til nægur jarðvarmi'
    # '...til að koma upp veggspjöldum'
    # '...séu um 20 kaupendur'
    # '...keyptu síðan félagið'
    # '...varpaði fram þeirri spurningu'
    # '...samið við nær öll félögin'
    # '...kom út skýrsla'
    # '...að meðal hitastig'
    # '...haldið hefur úti bloggsíðu'
    # '...tóku saman gögn'
    # '...það á jafnframt dótturfélag'
    # '...næstum tvo áratugi'
    # '...afsalaði sér samt launum'
    # '...og færði því Markúsi Erni tómatsósu'
    # '...lifði af nútímalega meðferð'
    # '...varð fyrir neðan Pál'
    # '...og Snædís, þá laganemi, bauð sig fram'
    _NOT_NOT_EO = frozenset(["inn", "eftir", "of", "til", "upp", "um", "síðan", "fram", "nær", "nærri",
        "út", "meðal", "úti", "saman", "jafnframt", "næstum", "samt", "samtals", "því", "nokkuð", "af",
        "neðan", "þá" ])

    # Words that are not eligible for interpretation as proper names, even if they are capitalized
    _NOT_PROPER_NAME = frozenset(["ég", "þú", "hann", "hún", "það", "við", "þið", "þau",
        "þeir", "þær", "mér", "mig", "mín", "þig", "þér", "þín", "þeim", "þeirra", "þetta", "þessi",
        "í", "á", "af", "um", "að", "með", "til", "frá", "búist", "annars", "samkvæmt", "en", "og",
        "sem"])

    # Numbers that can be used in the singular even if they are nominally plural.
    # This applies to the media company 365, where it is OK to say "365 skuldaði 389 milljónir",
    # as it would be incorrect to say "365 skulduðu 389 milljónir".
    _SINGULAR_SPECIAL_CASES = frozenset([ 365 ])

    _UNDERSTOOD_PUNCTUATION = ".?!,:;–-()[]"

    _MEANING_CACHE = { }

    def __init__(self, t, original_index):

        Token.__init__(self, TOK.descr[t[0]], t[1])
        self.t0 = t[0] # Token type (TOK.WORD, etc.)
        self.t1 = t[1] # Token text
        self.t1_lower = t[1].lower() # Token text, lower case
        # t2 contains auxiliary token information, such as part-of-speech annotation, numbers, etc.
        if self.t0 == TOK.ENTITY:
            # Cut off the entity definitions (not used during parse or stored with the tree)
            self.t2 = (None, t[2][1], t[2][2])
        elif isinstance(t[2], list):
            self.t2 = tuple(t[2])
        else:
            self.t2 = t[2]
        self.is_upper = self.t1[0] != self.t1_lower[0] # True if starts with upper case
        self._hash = None # Cached hash
        self._index = original_index # Index of original token within sentence

        # We store a cached check of whether this is an "eo". An "eo" is an adverb (atviksorð)
        # that cannot also be a preposition ("fs") and is therefore a possible non-ambiguous
        # prefix to a noun ("einkunn")
        self._is_eo = None

    @property
    def lower(self):
        """ Return the text for this property, in lower case """
        return self.t1_lower

    @property
    def index(self):
        """ Return the original index of the corresponding token within the sentence """
        return self._index

    @property
    def dump(self):
        """ Serialize the token as required for text dumping of trees """
        if self.t0 == TOK.WORD:
            # Simple case; no token kind or auxiliary information dumped
            return '"' + self.t1 + '"'
        if self.t2 is None:
            return '"{0}" {1}'.format(self.t1, self._kind)
        return '"{0}" {1} {2}'.format(self.t1, self._kind,
            json.dumps(self.t2, ensure_ascii = False))

    @classmethod
    def fbits(cls, beyging):
        """ Convert a 'beyging' field from BIN to a set of fbits """
        bit = cls.FBIT
        return reduce(lambda x, y: x | y, (b for key, b in bit.items() if key in beyging), 0)

    @classmethod
    def get_fbits(cls, beyging):
        """ Get the (cached) fbits for a BIN 'beyging' field """
        fbits = cls._MEANING_CACHE.get(beyging)
        if fbits is None:
            # Calculate a set of bits that represent the variants
            # present in m.beyging
            fbits = cls.fbits(beyging)
            cls._MEANING_CACHE[beyging] = fbits
        return fbits

    @staticmethod
    def mm_verb_stem(verb):
        """ Lookup a verb stem for a 'miðmynd' verb,
            i.e. "eignast" for "eiga" (which may have appeared
            as "eignaðist" in the text) """
        for fm in BIN_Db.get_db().lookup_forms_from_stem(verb):
            if fm.beyging == "MM-NH":
                # The MM-NH form is the canonical (nominal) form
                return fm.ordmynd
        # No MM canonical form found: return the original (normal) stem
        return verb

    @staticmethod
    def verb_matches(verb, terminal, form):
        """ Return True if the verb in question matches the verb category,
            where the category is one of so_0, so_1, so_2 depending on
            the allowable number of noun phrase arguments """

        # If this is an unknown but potentially composite verb,
        # it will contain one or more hyphens. In this case, use only
        # the last part in lookups in the internal verb lexicons.
        if "-" in verb:
            verb = verb.split("-")[-1]

        if terminal.is_subj:
            # Verb subject in non-nominative case
            # Examples:
            # 'Mig langar að fara til Frakklands'
            # 'Páli þykir þetta vera tóm vitleysa'
            if terminal.is_nh:
                if "NH" not in form:
                    # Nominative mode (nafnháttur)
                    return False
            if terminal.is_mm:
                # Central form of verb ('miðmynd')
                # For subj_mm, we don't care about anything but MM
                return "MM" in form
            if terminal.is_gm:
                # Central form of verb ('miðmynd')
                if "GM" not in form:
                    return False
            if terminal.is_singular and not "ET" in form:
                # Require singular
                return False
            if terminal.is_plural and not "FT" in form:
                # Require plural
                return False

            def subject_matches(subj):
                """ Look up the verb in the subjects list loaded from Verbs.conf """
                return subj in VerbSubjects.VERBS.get(verb, set())

            form_lh = "LHÞT" in form
            if terminal.is_lh:
                return form_lh and subject_matches("lhþt")
            # Don't allow lhþt unless explicitly requested in terminal
            if form_lh:
                return False
            form_sagnb = "SAGNB" in form
            if terminal.has_variant("none"):
                # subj_none: Check that the verb is listed in the 'none'
                # subject list in Verbs.conf
                if terminal.is_sagnb != form_sagnb:
                    return False
                return subject_matches("none")
            if form_sagnb and not terminal.is_sagnb:
                # For regular subj, we don't allow SAGNB form
                # ('langað', 'þótt')
                return False
            if terminal.has_variant("op") and not "OP" in form:
                return False
            # Make sure that the subject case (last variant) matches the terminal
            return subject_matches(terminal.variant(-1))

        if terminal.is_singular and "FT" in form:
            # Can't use plural verb if singular terminal
            return False
        if terminal.is_plural and "ET" in form:
            # Can't use singular verb if plural terminal
            return False
        # Check that person (1st, 2nd, 3rd) and other variant requirements match
        for v in terminal.variants:
            # Lookup variant to see if it is one of the required ones for verbs
            rq = BIN_Token._VERB_FORMS.get(v)
            if rq is not None and not rq in form:
                # If this is required variant that is not found in the form we have,
                # return False
                return False
        # Check restrictive variants, i.e. we don't accept meanings
        # that have those unless they are explicitly present in the terminal
        for v in [ "sagnb", "lhþt", "bh" ]: # Be careful with "lh" here - !!! add mm?
            if BIN_Token.VARIANT[v] in form and not terminal.has_variant(v):
                return False
        if terminal.is_lh:
            if "VB" in form and not terminal.has_variant("vb"):
                # We want only the strong declinations ("SB") of lhþt, not the weak ones,
                # unless explicitly requested
                return False
        if terminal.has_variant("bh") and "ST" in form:
            # We only want the explicit request forms (boðháttur), i.e. "bónaðu"/"bónið",
            # not "bóna" which causes ambiguity vs. the nominal mode (nafnháttur)
            return False
        # Check whether the verb token can potentially match the argument number
        # of the terminal in question. If the verb is known to take fewer
        # arguments than the terminal wants, this is not a match.
        if terminal.variant(0) not in "012":
            # No argument number: all verbs match, except...
            if terminal.is_lh:
                # Special check for lhþt: may specify a case without it being an argument case
                if any(terminal.has_variant(c) and BIN_Token.VARIANT[c] not in form for c in BIN_Token.CASES):
                    # Terminal specified a non-argument case but the token doesn't have it:
                    # no match
                    return False
            return True
        is_mm = "MM" in form
        nargs = int(terminal.variant(0))
        if is_mm:
            # For MM forms, do not use the normal stem of the verb
            # for lookup in the VerbObjects.VERBS collection;
            # instead, use the MM-NH stem.
            # This means that for instance "eignaðist hest" is not resolved
            # to "eiga" but to "eignast"
            verb = BIN_Token.mm_verb_stem(verb)
        if verb in VerbObjects.VERBS[nargs]:
            # Seems to take the correct number of arguments:
            # do a further check on the supported cases
            if nargs == 0:
                # Zero arguments: that's simple
                return True
            # Does this terminal require argument cases?
            if terminal.num_variants < 2:
                # No: we don't need to check further
                return True
            # The following is not consistent as some verbs take
            # legitimate arguments in 'miðmynd', such as 'krefjast', 'ábyrgjast'
            # 'undirgangast', 'minnast'. They are also not consistently
            # annotated in BIN; some of them are marked as MM and some not.
            if nargs > 1 and is_mm:
                # Temporary compromise: Don't accept verbs in 'miðmynd'
                # if taking >1 arguments
                return False
            # Check whether the parameters of this verb
            # match up with the requirements of the terminal
            # as specified in its variants at indices 1 and onward
            for argspec in VerbObjects.VERBS[nargs][verb]:
                if all(terminal.variant(1 + ix) == c for ix, c in enumerate(argspec)):
                    # All variants match this spec: we're fine
                    return True
            # No match: return False
            return False
        # It's not there with the correct number of arguments:
        # see if it definitely has fewer ones
        for i in range(0, nargs):
            if verb in VerbObjects.VERBS[i]:
                # Prevent verb from matching a terminal if it
                # doesn't have all the arguments that the terminal requires
                return False
        # !!! TEMPORARY code while the verb lexicon is being built
        # unknown = True
        # for i in range(nargs + 1, 3):
        #     if verb in VerbObjects.VERBS[i]:
        #         # The verb is known but takes more arguments than this
        #         unknown = False
        # # Unknown verb or arguments not too many: consider this a match
        # if unknown:
        #     # Note the unknown verb
        #     UnknownVerbs.add(verb)
        return True

    def matches_PERSON(self, terminal):
        """ Handle a person name token, matching it with a person_[case]_[gender] terminal """
        if terminal.startswith("sérnafn"):
            # We allow a simple person name to match an entity name (sérnafn)
            if not self.is_upper or " " in self.lower:
                # Must be capitalized and a single name
                return False
            if not terminal.num_variants:
                return True
            case = terminal.variant(0)
            return any(m.case == case for m in self.t2)
        if not terminal.startswith("person"):
            return False
        if not terminal.num_variants:
            # No variant specified on terminal: we're done
            if Settings.DEBUG:
                print("Matching person terminal, token.t2 is {0}".format(self.t2))
            return True
        # Check each PersonName tuple in the t2 list
        case = terminal.variant(0)
        gender = terminal.variant(1) if terminal.num_variants > 1 else None
        return any(case == m.case and (gender is None or gender == m.gender) for m in self.t2)

    def matches_ENTITY(self, terminal):
        """ Handle an entity name token, matching it with an entity terminal """
        return terminal.startswith("entity")

    def matches_PUNCTUATION(self, terminal):
        """ Match a literal terminal with the same content as the punctuation token """
        return terminal.matches("punctuation", self.t1, self.t1)

    def matches_CURRENCY(self, terminal):
        """ A currency name token matches a noun terminal """
        if not terminal.startswith("no"):
            return False
        if terminal.is_abbrev:
            # A currency does not match an abbreviation
            return False
        if self.t2[1]:
            # See whether any of the allowed cases match the terminal
            for c in BIN_Token.CASES:
                if terminal.has_variant(c) and c not in self.t2[1]:
                    return False
        if self.t2[2]:
            # See whether any of the allowed genders match the terminal
            for g in BIN_Token.GENDERS:
                if terminal.has_variant(g) and g not in self.t2[2]:
                    return False
        else:
            # Match only the neutral gender if no gender given
            # return not (terminal.has_variant("kk") or terminal.has_variant("kvk"))
            return not terminal.has_any_vbits(BIN_Token.VBIT_KK | BIN_Token.VBIT_KVK)
        return True

    def is_correct_singular_or_plural(self, terminal):
        """ Match a number with a singular or plural noun (terminal).
            In Icelandic, all integers whose modulo 100 ends in 1 are
            singular, except 11. """
        singular = False
        orig_i = i = int(self.t2[0])
        if float(i) == float(self.t2[0]):
            # Whole number (integer): may be singular
            i = abs(i) % 100
            singular = (i != 11) and (i % 10) == 1
        if terminal.is_singular and not singular:
            # Terminal is singular but number is plural
            return True if orig_i in BIN_Token._SINGULAR_SPECIAL_CASES else False
        if terminal.is_plural and singular:
            # Terminal is plural but number is singular
            return False
        return True

    def matches_NUMBER(self, terminal):
        """ A number token matches a number (töl) or noun terminal """

        no_info = not self.t2[1] and not self.t2[2]

        if no_info:
            if terminal.startswith("tala"):
                # Plain number with no case or gender info
                return self.is_correct_singular_or_plural(terminal)
            # If no case and gender info, we only match "tala",
            # not nouns or "töl" terminals
            return False

        if terminal.first not in {"töl", "to"}:
            return False
        if not self.is_correct_singular_or_plural(terminal):
            return False
        if terminal.startswith("to"):
            # Allow a match with "to" if we have both case and gender info
            if not self.t2[1] or not self.t2[2]:
                return False
            # Only check gender for "to", not "töl"
            for g in BIN_Token.GENDERS:
                if terminal.has_variant(g) and g not in self.t2[2]:
                    return False
        if self.t2[1]:
            # See whether any of the allowed cases match the terminal
            for c in BIN_Token.CASES:
                if terminal.has_variant(c) and c not in self.t2[1]:
                    return False
        return True

    def matches_AMOUNT(self, terminal):
        """ An amount token matches a noun terminal """
        if not terminal.startswith("no"):
            return False
        if terminal.has_any_vbits(BIN_Token.VBIT_ABBREV | BIN_Token.VBIT_GR):
            # An amount does not match an abbreviation or
            # a definite article
            return False
        if not self.is_correct_singular_or_plural(terminal):
            return False
        #print("matches_AMOUNT terminal {0}, amt cases {1} genders {2}".format(terminal, self.t2[2], self.t2[3]))
        if self.t2[2]:
            # See whether any of the allowed cases match the terminal
            for c in BIN_Token.CASES:
                if terminal.has_variant(c) and c not in self.t2[2]:
                    return False
        if self.t2[3] is None:
            # No gender: match neutral gender only
            if terminal.has_any_vbits(BIN_Token.VBIT_KK | BIN_Token.VBIT_KVK):
                return False
        else:
            # Associated gender
            for g in BIN_Token.GENDERS:
                if terminal.has_variant(g) and g not in self.t2[3]:
                    return False
        return True

    def matches_PERCENT(self, terminal):
        """ A percent token matches a number (töl) or noun terminal """
        if not terminal.startswith("töl"):
            # Matches number and noun terminals only
            if not terminal.startswith("no"):
                return False
            if terminal.is_abbrev:
                return False
            # If we are recognizing this as a noun, do so only with neutral gender
            if not terminal.has_variant("hk"):
                return False
            if terminal.has_variant("gr"):
                return False
            if self.t2[1]:
                # See whether any of the allowed cases match the terminal
                for c in BIN_Token.CASES:
                    if terminal.has_variant(c) and c not in self.t2[1]:
                        return False
        # We do not check singular or plural here since phrases such as
        # '35% skattur' and '1% allra blóma' are valid
        return True

    def matches_YEAR(self, terminal):
        """ A year token matches a number (töl) or year (ártal) terminal """
        if terminal.first not in {"töl", "ártal", "tala"}:
            return False
        # Only singular match ('2014 var gott ár', not '2014 voru góð ár')
        # Years only match the neutral gender
        if terminal.has_any_vbits(BIN_Token.VBIT_FT | BIN_Token.VBIT_KK | BIN_Token.VBIT_KVK):
            return False
        # No case associated with year numbers: match all
        return True

    def matches_DATE(self, terminal):
        """ A date token matches a date (dags) terminal """
        return terminal.startswith("dags")

    def matches_TIME(self, terminal):
        """ A time token matches a time (tími) terminal """
        return terminal.startswith("tími")

    def matches_TIMESTAMP(self, terminal):
        """ A timestamp token matches a timestamp (tímapunktur) terminal """
        return terminal.startswith("tímapunktur")

    def matches_ORDINAL(self, terminal):
        """ An ordinal token matches an ordinal (raðnr) terminal """
        return terminal.startswith("raðnr")

    def matches_WORD(self, terminal):
        """ Match a word token, having the potential part-of-speech meanings
            from the BIN database, with the terminal """

        def matcher_so(m):
            """ Check verb """
            if m.ordfl != "so":
                return False
            # Special case for verbs: match only the appropriate
            # argument number, i.e. so_0 for verbs having no noun argument,
            # so_1 for verbs having a single noun argument, and
            # so_2 for verbs with two noun arguments. A verb may
            # match more than one argument number category.
            return self.verb_matches(m.stofn, terminal, m.beyging)

        def matcher_no(m):
            """ Check noun """
            if BIN_Token._KIND[m.ordfl] != "no":
                return False
            no_info = m.beyging == "-"
            if terminal.is_abbrev:
                # Only match abbreviations; gender, case and number do not matter
                return no_info
            if m.fl == "nafn":
                # Names are only matched by person terminals
                return False
            for v in terminal.variants:
                if v in BIN_Token.GENDERS_SET:
                    if m.ordfl != v:
                        # Mismatched gender
                        return False
                elif no_info:
                    # No case and number info: probably a foreign word
                    # Match all cases and numbers
                    #if v == "ft":
                    #    return False
                    if v == "gr":
                        # Do not match a demand for the definitive article ('greinir')
                        return False
                elif BIN_Token.VARIANT[v] not in m.beyging:
                    # Required case or number not found: no match
                    return False
            return True

        def matcher_gata(m):
            """ Check street name """
            if m.fl != "göt": # Götuheiti
                return False
            if BIN_Token._KIND[m.ordfl] != "no":
                return False
            for v in terminal.variants:
                if v in BIN_Token.GENDERS_SET:
                    if m.ordfl != v:
                        # Mismatched gender
                        return False
                elif BIN_Token.VARIANT[v] not in m.beyging:
                    # Required case or number not found: no match
                    return False
            return True

        def matcher_eo(m):
            """ 'Einkunnarorð': adverb (atviksorð) that is not the same
                as a preposition (forsetning) """
            if m.ordfl != "ao":
                return False
            # This token can match an adverb:
            # Cache whether it can also match a preposition
            if self._is_eo is None:
                if self.t1_lower in BIN_Token._NOT_EO:
                    # Explicitly forbidden, no need to check further
                    self._is_eo = False
                elif self.t1_lower in BIN_Token._NOT_NOT_EO:
                    # Explicitly allowed, no need to check further
                    self._is_eo = True
                else:
                    # Check whether also a preposition or pronoun and return False in that case
                    self._is_eo = not any(mm.ordfl in {"fs", "fn"} for mm in self.t2)
            # Return True if this token cannot also match a preposition
            return self._is_eo

        def matcher_fs(m):
            """ Check preposition """
            if not terminal.num_variants:
                return False
            # Note that in the case of abbreviated prepositions,
            # such as 'skv.' for 'samkvæmt', the full expanded form
            # is found in m.stofn - not self.t1_lower or m.ordmynd
            fs = self.t1_lower
            if '.' in fs:
                fs = m.stofn
            # !!! Note that this will match a word and return True even if the
            # meanings of the token (the list in self.t2) do not include
            # the fs category. This effectively makes the prepositions
            # exempt from the ambiguous_phrases optimization.
            return fs in Prepositions.PP and terminal.variant(0) in Prepositions.PP[fs]

        def matcher_person(m):
            """ Check name from static phrases, coming from the Reynir.conf file """
            if m.fl != "nafn":
                return False
            if terminal.has_vbits(BIN_Token.VBIT_HK):
                # Never match neutral terminals
                return False
            # Check case, if present
            if m.beyging != "-":
                if any(BIN_Token.VARIANT[c] in m.beyging and not terminal.has_variant(c) for c in BIN_Token.CASES):
                    # The name has an associated case, but this is not it: quit
                    return False
            if terminal.has_vbits(BIN_Token.VBIT_KK) and m.ordfl != "kk":
                # Masculine specified but the name is feminine: no match
                return False
            if terminal.has_vbits(BIN_Token.VBIT_KVK) and m.ordfl != "kvk":
                # Feminine specified but the name is masculine: no match
                return False
            return True

        def matcher_corporation(m):
            """ Check whether the token text matches a set of corporation identfiers """
            # Note: these must have a meaning for this to work, so specifying them
            # as abbreviations to Main.conf is recommended
            return self.t1 in {
                "ehf.", "ehf", "hf.", "hf",
                "bs.", "bs", "sf.", "sf", "slhf.", "slhf", "slf.", "slf", "svf.", "svf", "ohf.", "ohf",
                "Inc", "Inc.", "Incorporated",
                "Corp", "Corp.", "Corporation",
                "Ltd", "Ltd.", "Limited",
                "Co", "Co.", "Company",
                "AS", "ASA",
                "SA", "S.A.",
                "GmbH", "AG",
                "SARL", "S.à.r.l."
            }

        def matcher_default(m):
            """ Check other word categories """
            if m.beyging == "-": # Tokens without a form specifier are assumed to be universally matching
                fbits = 0
            else:
                # If the meaning is a noun, its gender is coded in the ordfl attribute
                # In that case, add it to the beyging field so that the relevant fbits
                # are included and can be matched against the terminal if it requires
                # a gender
                fbits = BIN_Token.get_fbits(m.beyging + BIN_Token.GENDERS_MAP.get(m.ordfl, ""))
            # Check whether variants required by the terminal are present
            # in the meaning string
            if not terminal.fbits_match(fbits):
                return False
            return terminal.matches_first(m.ordfl, m.stofn, self.t1_lower)

        def matches_proper_name():
            # Proper name?
            # Only allow a potential interpretation as a proper name if
            # the token is uppercase but there is no uppercase meaning of
            # the word in BÍN. This excludes for instance "Ísland" which
            # should be treated purely as a noun, not as a proper name.
            #if any(m.ordmynd[0].isupper() and m.beyging != "-" for m in self.t2):
            #    return False
            if self.t1_lower in BIN_Token._NOT_PROPER_NAME:
                return False
            if " " in self.t1_lower:
                return False
            if not terminal.num_variants:
                return self.t2[0] if self.t2 else True # Return first meaning, or just plain True if no meanings
            # The terminal is sérnafn_case: We only accept nouns or adjectives
            # that match the given case
            for m in self.t2:
                fbits = BIN_Token.get_fbits(m.beyging) & BIN_Token.VBIT_CASES
                if BIN_Token._KIND[m.ordfl] in {"no", "lo"} and terminal.fbits_match(fbits):
                    return m # Return the matching meaning
            return False

        # We have a match if any of the possible part-of-speech meanings
        # of this token match the terminal
        if self.t2:
            # The dispatch table has to be constructed each time because
            # the calls will have a wrong self pointer otherwise
            matchers = {
                "so" : matcher_so,
                "no" : matcher_no,
                "eo" : matcher_eo,
                "fs" : matcher_fs,
                "person" : matcher_person,
                "gata" : matcher_gata, # Götuheiti = Street name
                "fyrirtæki" : matcher_corporation, # Company identifier, i.e. hf., ehf., Inc., Corp. etc.
                "sérnafn" : None
            }
            matcher = matchers.get(terminal.first, matcher_default)
            if matcher:
                # Return the first matching meaning, or False if none
                return next((m for m in self.t2 if matcher(m)), False)
            # Terminal is a proper name ('sérnafn')
            return self.is_upper and matches_proper_name()

        # Unknown word, i.e. no meanings in BÍN (might be foreign, unknown name, etc.)
        if self.is_upper:
            # Starts in upper case: We allow this to match a named entity terminal ('sérnafn')
            return terminal.startswith("sérnafn")

        # Not upper case: allow it to match a singular, neutral noun in all cases,
        # but without the definite article ('greinir')
        return terminal.startswith("no") and \
            terminal.has_vbits(BIN_Token.VBIT_ET | BIN_Token.VBIT_HK) and \
            not terminal.has_vbits(BIN_Token.VBIT_GR)

    # Dispatch table for the token matching functions
    _MATCHING_FUNC = {
        TOK.PERSON: matches_PERSON,
        TOK.ENTITY: matches_ENTITY,
        TOK.PUNCTUATION: matches_PUNCTUATION,
        TOK.CURRENCY: matches_CURRENCY,
        TOK.AMOUNT: matches_AMOUNT,
        TOK.NUMBER: matches_NUMBER,
        TOK.PERCENT: matches_PERCENT,
        TOK.ORDINAL: matches_ORDINAL,
        TOK.YEAR: matches_YEAR,
        TOK.DATE: matches_DATE,
        TOK.TIME: matches_TIME,
        TOK.TIMESTAMP: matches_TIMESTAMP,
        TOK.WORD: matches_WORD
    }

    @classmethod
    def is_understood(cls, t):
        """ Return True if the token type is understood by the BIN Parser """
        if t[0] == TOK.PUNCTUATION:
            # A limited number of punctuation symbols is currently understood
            return t[1] in cls._UNDERSTOOD_PUNCTUATION
        return t[0] in cls._MATCHING_FUNC

    def matches(self, terminal):
        """ Return True if this token matches the given terminal """
        # Dispatch the token matching according to the dispatch table in _MATCHING_FUNC
        return BIN_Token._MATCHING_FUNC[self.t0](self, terminal) is not False

    def match_with_meaning(self, terminal):
        """ Return False if this token does not match the given terminal;
            otherwise True or the actual meaning tuple that matched """
        # Dispatch the token matching according to the dispatch table in _MATCHING_FUNC
        return BIN_Token._MATCHING_FUNC[self.t0](self, terminal)

    def __repr__(self):
        return "[" + TOK.descr[self.t0] + ": " + self.t1 + "]"

    def __str__(self):
        return "\'" + self.t1 + "\'"

    @property
    def key(self):
        """ Return a hashable key that partitions tokens based on
            effective identity, i.e. tokens with the same hash can be considered
            equivalent for parsing purposes. This hash is inter alia used by the
            alloc_cache() function in fastparser.py to optimize token/terminal
            matching calls. """
        if self.t0 == TOK.WORD:
            # For words, the t2 tuple is significant because it may have been
            # cut down by the tokenizer due to the word's context, cf. the
            # [ambiguous_phrases] section in Main.conf
            return (self.t0, self.t1, self.t2)
        # Otherwise, the t0 and t1 fiels are enough
        return (self.t0, self.t1)

    def __hash__(self):
        """ Calculate and cache a hash for this token """
        if self._hash is None:
            self._hash = hash(self.key)
        return self._hash

    @classmethod
    def init(cls):
        # Initialize cached dictionary of verb variant forms in BIN
        cls._VERB_FORMS = { v : cls.VARIANT[v] for v in cls.VERB_VARIANTS }

BIN_Token.init()


class VariantHandler:

    """ Mix-in class used in BIN_Terminal and BIN_LiteralTerminal to add
        querying of terminal variants as well as mapping of variants to
        bit arrays for speed """

    def __init__(self, name):
        super().__init__(name)
        # Do a bit of pre-calculation to speed up various
        # checks against this terminal
        parts = self._name.split("_")
        self._first = parts[0]
        # The variant set for this terminal, i.e.
        # tname_var1_var2_var3 -> { 'var1', 'var2', 'var3' }
        self._vparts = parts[1:]
        self._vcount = len(self._vparts)
        self._vset = set(self._vparts)
        # Also map variant names to bits in self._vbits
        bit = BIN_Token.VBIT
        self._vbits = reduce(lambda x, y: x | y,
            (bit[v] for v in self._vset if v in bit), 0)
        # fbits are like vbits but leave out variants that have no BIN meaning
        self._fbits = self._vbits & (~BIN_Token.FBIT_MASK)
        # For speed, store the cases associated with a verb
        # so_0 -> self._cases = ""
        # so_1_þgf -> self._cases = "þgf"
        # so_2_þf_þgf -> self._cases = "þf_þgf"
        if self._vcount >= 1 and self._vparts[0] in "012":
            ncases = int(self._vparts[0])
            self._cases = "".join("_" + self._vparts[1 + i] for i in range(ncases))
        else:
            self._cases = ""

    def startswith(self, part):
        """ Returns True if the terminal name starts with the given string """
        return self._first == part

    def matches_first(self, t_kind, t_val, t_lit):
        """ Returns True if the first part of the terminal name matches the
            given word category """
        # Convert 'kk', 'kvk', 'hk' to 'no' before doing the compare
        return self._first == BIN_Token._KIND[t_kind]

    @property
    def first(self):
        """ Return the first part of the terminal name (without variants) """
        return self._first

    @property
    def num_variants(self):
        """ Return the number of variants in the terminal name """
        return self._vcount

    @property
    def variants(self):
        """ Returns the variants contained in this terminal name as a list """
        return self._vparts

    def variant(self, index):
        """ Return the variant with the given index """
        return self._vparts[index]

    @property
    def verb_cases(self):
        """ Return the verb cases associated with a so_ terminal, or empty string """
        return self._cases
    
    def has_variant(self, v):
        """ Returns True if the terminal name has the given variant """
        return v in self._vset

    def has_vbits(self, vbits):
        """ Return True if this terminal has (all) the variant(s) corresponding to the given bit(s) """
        return (self._vbits & vbits) == vbits

    def has_any_vbits(self, vbits):
        """ Return True if this terminal has any of the variant(s) corresponding to the given bit(s) """
        return (self._vbits & vbits) != 0

    def fbits_match(self, fbits):
        """ Return True if the given fbits meet all variant criteria """
        # That is: for every bit in self._fbits, there must be a corresponding bit
        # in the given fbits. We test this by turning off all the bits given in the
        # parameter fbits and checking whether there are any bits left.
        return (self._fbits & ~fbits) == 0

    @property
    def gender(self):
        """ Return a gender string corresponding to a variant of this terminal, if any """
        if self._vbits & BIN_Token.VBIT_KK:
            return "kk"
        if self._vbits & BIN_Token.VBIT_KVK:
            return "kvk"
        if self._vbits & BIN_Token.VBIT_HK:
            return "hk"
        return None

    @property
    def is_singular(self):
        return (self._vbits & BIN_Token.VBIT_ET) != 0

    @property
    def is_plural(self):
        return (self._vbits & BIN_Token.VBIT_FT) != 0

    @property
    def is_abbrev(self):
        return (self._vbits & BIN_Token.VBIT_ABBREV) != 0

    @property
    def is_nh(self):
        return (self._vbits & BIN_Token.VBIT_NH) != 0

    @property
    def is_mm(self):
        return (self._vbits & BIN_Token.VBIT_MM) != 0

    @property
    def is_gm(self):
        return (self._vbits & BIN_Token.VBIT_GM) != 0

    @property
    def is_subj(self):
        return (self._vbits & BIN_Token.VBIT_SUBJ) != 0

    @property
    def is_sagnb(self):
        return (self._vbits & BIN_Token.VBIT_SAGNB) != 0

    @property
    def is_lh(self):
        return (self._vbits & BIN_Token.VBIT_LH) != 0

    @property
    def is_vh(self):
        return (self._vbits & BIN_Token.VBIT_VH) != 0


class BIN_Terminal(VariantHandler, Terminal):

    """ Subclass of Terminal that mixes in support from VariantHandler
        for variants in terminal names, including optimizations of variant
        checks and lookups """

    def __init__(self, name):
        super().__init__(name)


class BIN_LiteralTerminal(VariantHandler, LiteralTerminal):

    """ Subclass of LiteralTerminal that mixes in support from VariantHandler
        for variants in terminal names """

    def __init__(self, name):
        super().__init__(name)
        # Peel off the quotes from the first part
        assert len(self._first) >= 3
        assert self._first[0] == self._first[-1]
        self._first = self._first[1:-1]
        self._cat = None
        if len(self._first) > 1:
            # Check for a word category specification,
            # i.e. "sem:st", "að:fs", 'vera:so'_gm_nt
            a = self._first.split(':')
            if len(a) > 2:
                raise GrammarError("A literal terminal can only have one word category specification")
            elif len(a) == 2:
                # We have a word category specification
                self._first = a[0]
                self._cat = a[1]
        # Check whether we have variants on an exact literal
        if self._strong and self.num_variants > 0:
            # It doesn't make sense to have variants on exact literals
            # since they are constant and cannot vary
            raise GrammarError('An exact literal terminal with double quotes cannot have variants')

    @property
    def cat(self):
        return self._cat

    def matches_first(self, t_kind, t_val, t_lit):
        """ A literal terminal matches a token if the token text is identical to the literal """
        if self._cat is not None and t_kind != self._cat:
            # Match only the word category that was specified
            return False
        return (self._first == t_lit) if self._strong else (self._first == t_val)

    def matches(self, t_kind, t_val, t_lit):
        """ A literal terminal matches a token if the token text is
            canonically or absolutely identical to the literal """
        return (self._first == t_lit) if self._strong else (self._first == t_val)


class BIN_Nonterminal(Nonterminal):

    def __init__(self, name, fname, line):
        super().__init__(name, fname, line)
        # Optimized check for whether this is a noun phrase nonterminal
        self._is_noun_phrase = name.startswith("Nl_")

    @property
    def is_noun_phrase(self):
        """ Return True if this nonterminal denotes a noun phrase """
        return self._is_noun_phrase


class BIN_Grammar(Grammar):

    """ Subclass of Grammar that creates BIN-specific Terminals and LiteralTerminals
        when parsing a grammar, with support for variants in terminal names """

    def __init__(self):
        super().__init__()

    @staticmethod
    def _make_terminal(name):
        """ Make BIN_Terminal instances instead of plain-vanilla Terminals """
        return BIN_Terminal(name)

    @staticmethod
    def _make_literal_terminal(name):
        """ Make BIN_LiteralTerminal instances instead of plain-vanilla LiteralTerminals """
        return BIN_LiteralTerminal(name)

    @staticmethod
    def _make_nonterminal(name, fname, line):
        """ Make BIN_Terminal instances instead of plain-vanilla Terminals """
        return BIN_Nonterminal(name, fname, line)


class BIN_Parser(Base_Parser):

    """ BIN_Parser parses sentences according to the Icelandic
        grammar in the Reynir.grammar file. It subclasses Parser
        and wraps the interface between the BIN grammatical
        data on one hand and the tokens and grammar terminals on
        the other. """

    # A singleton instance of the parsed Reynir.grammar
    _grammar = None
    _grammar_ts = None

    # BIN_Parser version - change when logic is modified so that it
    # affects the parse tree
    _VERSION = "1.0"
    _GRAMMAR_FILE = "Reynir.grammar"

    def __init__(self, verbose = False):
        """ Load the shared BIN grammar if not already there, then initialize
            the Base_Parser parent class """
        g = BIN_Parser._grammar
        ts = os.path.getmtime(BIN_Parser._GRAMMAR_FILE)
        if g is None or BIN_Parser._grammar_ts != ts:
            # Grammar not loaded, or its timestamp has changed: load it
            t0 = time.time()
            g = BIN_Grammar()
            if Settings.DEBUG:
                print("Loading grammar file {0} with timestamp {1}".format(BIN_Parser._GRAMMAR_FILE, datetime.fromtimestamp(ts)))
            g.read(BIN_Parser._GRAMMAR_FILE, verbose = verbose)
            BIN_Parser._grammar = g
            BIN_Parser._grammar_ts = ts
            if Settings.DEBUG:
                print("Grammar parsed and loaded in {0:.2f} seconds".format(time.time() - t0))
        super().__init__(g)

    @property
    def grammar(self):
        """ Return the grammar loaded from Reynir.grammar """
        return BIN_Parser._grammar

    @property
    def version(self):
        """ Return a composite version string from BIN_Parser and Parser """
        ftime = str(self.grammar.file_time)[0:19] # YYYY-MM-DD HH:MM:SS
        return ftime + "/" + BIN_Parser._VERSION + "/" + super()._VERSION

    @staticmethod
    def _wrap(tokens):
        """ Sanitize the 'raw' tokens and wrap them in BIN_Token() wrappers """

        # Remove stuff that won't be understood in any case
        # Start with runs of unknown words inside parentheses
        tlist = list(tokens)
        tlen = len(tlist)

        def scan_par(left):
            """ Scan tokens inside parentheses and remove'em all
                if they are only unknown words - perhaps starting with
                an abbreviation """
            right = left + 1
            while right < tlen:
                tok = tlist[right]
                if tok[0] == TOK.PUNCTUATION and tok[1] == ')':
                    # Check the contents of the token list from left+1 to right-1

                    # Skip parentheses starting with "e." (English), "þ." (German) or "d." (Danish)
                    foreign = right > left + 1 and tlist[left + 1][1] in { "e.", "d.", "þ." }

                    def is_unknown(t):
                        """ A token is unknown if it is a TOK.UNKNOWN or if it is a
                            TOK.WORD with no meanings """
                        UNKNOWN = { "e.", "d.", "þ.", "t.d.", "þ.e.", "m.a." } # Abbreviations and stuff that we ignore inside parentheses
                        return t[0] == TOK.UNKNOWN or (t[0] == TOK.WORD and not t[2]) or t[1] in UNKNOWN

                    if foreign or all(is_unknown(t) for t in tlist[left+1:right]):
                        # Only unknown tokens: erase'em, including the parentheses
                        for ix in range(left, right + 1):
                            tlist[ix] = None

                    return right + 1

                right += 1
            # No match: we're done
            return right

        ix = 0
        while ix < tlen:
            tok = tlist[ix]
            if tok[0] == TOK.PUNCTUATION and tok[1] == '(':
                ix = scan_par(ix) # Jumps to the right parenthesis, if found
            else:
                ix += 1

        # Wrap the sanitized token list in BIN_Token()
        # while keeping a back index to the original token
        wrapped_tokens = [ ]
        for ix, t in enumerate(tlist):
            if t is not None and BIN_Token.is_understood(t):
                wrapped_tokens.append(BIN_Token(t, ix))
        return wrapped_tokens

    def go(self, tokens):
        """ Parse the token list after wrapping each understood token in the BIN_Token class """
        raise NotImplementedError # This should never be called - is overridden in Fast_Parser
